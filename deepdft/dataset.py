from typing import List
import warnings
import tarfile
import tempfile
import multiprocessing
import queue
import time
import threading
import logging
import zlib
import os
import io
import math
import torch
import lz4.frame
import numpy as np
import ase
import ase.io.cube
from ase.calculators.vasp import VaspChargeDensity
import asap3

from deepdft.layer import pad_and_stack


def rotating_pool_worker(dataset, rng, queue):
    while True:
        for index in rng.permutation(len(dataset)):
            queue.put(dataset[index])


def transfer_thread(queue: multiprocessing.Queue, datalist: list):
    while True:
        for index in range(len(datalist)):
            datalist[index] = queue.get()


class RotatingPoolData(torch.utils.data.Dataset):
    """
    Wrapper for a dataset that continously loads data into a smaller pool.
    The data loading is performed in a separate process and is assumed to be IO bound.
    """

    def __init__(self, dataset, pool_size, **kwargs):
        super().__init__(**kwargs)
        self.pool_size = pool_size
        self.parent_data = dataset
        self.rng = np.random.default_rng()
        logging.debug("Filling rotating data pool of size %d" % pool_size)
        self.data_pool = [
            self.parent_data[i]
            for i in self.rng.integers(
                0, high=len(self.parent_data), size=self.pool_size, endpoint=False
            )
        ]
        self.loader_queue = multiprocessing.Queue(2)

        # Start loaders
        self.loader_process = multiprocessing.Process(
            target=rotating_pool_worker,
            args=(self.parent_data, self.rng, self.loader_queue),
        )
        self.transfer_thread = threading.Thread(
            target=transfer_thread, args=(self.loader_queue, self.data_pool)
        )
        self.loader_process.start()
        self.transfer_thread.start()

    def __len__(self):
        return self.pool_size

    def __getitem__(self, index):
        return self.data_pool[index]


class BufferData(torch.utils.data.Dataset):
    """
    Wrapper for a dataset. Loads all data into memory.
    """

    def __init__(self, dataset, **kwargs):
        super().__init__(**kwargs)

        self.data_objects = [dataset[i] for i in range(len(dataset))]

    def __len__(self):
        return len(self.data_objects)

    def __getitem__(self, index):
        return self.data_objects[index]


class DensityData(torch.utils.data.Dataset):
    def __init__(self, tarpath, **kwargs):
        super().__init__(**kwargs)

        self.tarpath = tarpath
        self.member_list = []

        # Index tar file
        with tarfile.open(self.tarpath, "r:") as tar:
            for member in tar.getmembers():
                self.member_list.append(member)

    def __len__(self):
        return len(self.member_list)

    def extract_member(self, tarinfo):
        with tarfile.open(self.tarpath, "r") as tar:
            filecontent = _decompress(tar, tarinfo)
            if tarinfo.name.endswith((".cube", ".cube.zz", "cube.lz4")):
                density, atoms, origin = _read_cube(filecontent)
            else:
                density, atoms, origin = _read_vasp(filecontent)

        grid_pos = _calculate_grid_pos(density, origin, atoms.get_cell())

        metadata = {"filename": tarinfo.name}
        return {
            "density": density,
            "atoms": atoms,
            "origin": origin,
            "grid_position": grid_pos,
            "metadata": metadata, # Meta information
        }

    def __getitem__(self, index):
        return self.extract_member(self.member_list[index])


class AseNeigborListWrapper:
    """
    Wrapper around ASE neighborlist to have the same interface as asap3 neighborlist

    """

    def __init__(self, cutoff, atoms):
        self.neighborlist = ase.neighborlist.NewPrimitiveNeighborList(
            cutoff, skin=0.0, self_interaction=False, bothways=True
        )
        self.neighborlist.build(
            atoms.get_pbc(), atoms.get_cell(), atoms.get_positions()
        )
        self.cutoff = cutoff
        self.atoms_positions = atoms.get_positions()
        self.atoms_cell = atoms.get_cell()

    def get_neighbors(self, i, cutoff):
        assert (
            cutoff == self.cutoff
        ), "Cutoff must be the same as used to initialise the neighborlist"

        indices, offsets = self.neighborlist.get_neighbors(i)

        rel_positions = (
            self.atoms_positions[indices]
            + offsets @ self.atoms_cell
            - self.atoms_positions[i][None]
        )

        dist2 = np.sum(np.square(rel_positions), axis=1)

        return indices, rel_positions, dist2


def grid_iterator_worker(atoms, meshgrid, probe_count, cutoff, slice_id_queue, result_queue):
    try:
        neighborlist = asap3.FullNeighborList(cutoff, atoms)
    except Exception as e:
        warnings.warn("Failed to create asap3 neighborlist, this might get slow. Error: %s", e)
        neighborlist = None
    while True:
        try:
            slice_id = slice_id_queue.get(True, 10)
        except queue.Empty:
            while not result_queue.empty():
                time.sleep(1)
            result_queue.close()
            return 0
        res = DensityGridIterator.static_get_slice(slice_id, atoms, meshgrid, probe_count, cutoff, neighborlist=neighborlist)
        result_queue.put((slice_id, res))

class DensityGridIterator:
    def __init__(self, densitydict, ignore_pbc: bool, probe_count: int, cutoff: float):
        num_positions = np.prod(densitydict["grid_position"].shape[0:3])
        self.num_slices = int(math.ceil(num_positions / probe_count))
        self.probe_count = probe_count
        self.cutoff = cutoff
        self.ignore_pbc = ignore_pbc

        if ignore_pbc:
            self.atoms = densitydict["atoms"].copy()
            self.atoms.set_pbc(False)
        else:
            self.atoms = densitydict["atoms"]

        self.meshgrid = densitydict["grid_position"]

    def get_slice(self, slice_index):
        return self.static_get_slice(slice_index, self.atoms, self.meshgrid, self.probe_count, self.cutoff)

    @staticmethod
    def static_get_slice(slice_index, atoms, meshgrid, probe_count, cutoff, neighborlist=None):
        num_positions = np.prod(meshgrid.shape[0:3])
        flat_index = np.arange(slice_index*probe_count, min((slice_index+1)*probe_count, num_positions))
        pos_index = np.unravel_index(flat_index, meshgrid.shape[0:3])
        probe_pos = meshgrid[pos_index]
        probe_edges, probe_edges_features = probes_to_graph(atoms, probe_pos, cutoff, neighborlist)

        if not probe_edges:
            probe_edges = [np.zeros((0,2), dtype=np.int)]
            probe_edges_features = [np.zeros((0,), dtype=np.float32)]

        res = {
            "probe_edges": np.concatenate(probe_edges, axis=0),
            "probe_edges_features": np.concatenate(probe_edges_features, axis=0).astype(np.float32)[:, None],
        }
        res["num_probe_edges"] = res["probe_edges"].shape[0]
        res["num_probes"] = len(flat_index)

        return res


    def __iter__(self):
        self.current_slice = 0
        slice_id_queue = multiprocessing.Queue()
        self.result_queue = multiprocessing.Queue(100)
        self.finished_slices = dict()
        for i in range(self.num_slices):
            slice_id_queue.put(i)
        self.workers = [multiprocessing.Process(target=grid_iterator_worker, args=(self.atoms, self.meshgrid, self.probe_count, self.cutoff, slice_id_queue, self.result_queue)) for _ in range(6)]
        for w in self.workers:
            w.start()
        return self

    def __next__(self):
        if self.current_slice < self.num_slices:
            this_slice = self.current_slice
            self.current_slice += 1

            # Retrieve finished slices until we get the one we are looking for
            while this_slice not in self.finished_slices:
                i, res = self.result_queue.get()
                res = {k: torch.tensor(v) for k,v in res.items()} # convert to torch tensor
                self.finished_slices[i] = res
            return self.finished_slices.pop(this_slice)
        else:
            for w in self.workers:
                w.join()
            raise StopIteration


### def atoms_and_probes_to_graph(atoms, probe_pos, cutoff):
###     # Insert probe atoms
###     num_probes = probe_pos.shape[0]
###     probe_atoms = ase.Atoms(numbers=[0] * num_probes, positions=probe_pos)
###     atoms_with_probes = atoms.copy()
###     atoms_with_probes.extend(probe_atoms)
### 
###     atom_edges = []
###     atom_edges_features = []
###     probe_edges = []
###     probe_edges_features = []
### 
###     # Compute neighborlist
###     if np.any(atoms.get_cell().lengths() <= 0.0001) or (np.any(atoms.get_pbc()) and np.any(atoms.get_cell().lengths() < cutoff)):
###         neighborlist = AseNeigborListWrapper(cutoff, atoms_with_probes)
###     else:
###         neighborlist = asap3.FullNeighborList(cutoff, atoms_with_probes)
###     atomic_numbers = atoms_with_probes.get_atomic_numbers()
###     for i in range(len(atoms_with_probes)):
###         neigh_idx, _, neigh_dist2 = neighborlist.get_neighbors(i, cutoff)
###         neigh_dist = np.sqrt(neigh_dist2)
###         neigh_atomic_species = atomic_numbers[neigh_idx]
### 
###         neigh_is_atom = neigh_atomic_species != 0
###         neigh_atoms = neigh_idx[neigh_is_atom]
### 
###         self_index = np.ones_like(neigh_atoms) * i
###         if i < len(atoms):
###             self_index = np.ones_like(neigh_atoms) * i
###         else:
###             self_index = np.ones_like(neigh_atoms) * (i - len(atoms))
###         edges = np.stack((neigh_atoms, self_index), axis=1)
### 
###         if i < len(atoms):  # We are computing edges for an atom
###             atom_edges.append(edges)
###             atom_edges_features.append(neigh_dist[neigh_is_atom])
###         else:  # We are computing edgs for a probe
###             probe_edges.append(edges)
###             probe_edges_features.append(neigh_dist[neigh_is_atom])
### 
###     return atom_edges, atom_edges_features, probe_edges, probe_edges_features

def atoms_and_probe_sample_to_graph_dict(density, atoms, grid_pos, cutoff, num_probes):
    # Sample probes on the calculated grid
    probe_choice_max = np.prod(grid_pos.shape[0:3])
    probe_choice = np.random.randint(probe_choice_max, size=num_probes)
    probe_choice = np.unravel_index(probe_choice, grid_pos.shape[0:3])
    probe_pos = grid_pos[probe_choice]
    probe_target = density[probe_choice]

    atom_edges, atom_edges_features, neighborlist = atoms_to_graph(atoms, cutoff)
    probe_edges, probe_edges_features = probes_to_graph(atoms, probe_pos, cutoff, neighborlist=neighborlist)

    default_type = torch.get_default_dtype()

    if not probe_edges:
        probe_edges = [np.zeros((0,2), dtype=np.int)]
        probe_edges_features = [np.zeros((0,), dtype=np.int)]
    # pylint: disable=E1102
    res = {
        "nodes": torch.tensor(atoms.get_atomic_numbers()),
        "atom_edges": torch.tensor(np.concatenate(atom_edges, axis=0)),
        "atom_edges_features": torch.tensor(
            np.concatenate(atom_edges_features, axis=0)[:, None], dtype=default_type
        ),
        "probe_edges": torch.tensor(np.concatenate(probe_edges, axis=0)),
        "probe_edges_features": torch.tensor(
            np.concatenate(probe_edges_features, axis=0)[:, None], dtype=default_type
        ),
        "probe_target": torch.tensor(probe_target, dtype=default_type),
    }
    res["num_nodes"] = torch.tensor(res["nodes"].shape[0])
    res["num_atom_edges"] = torch.tensor(res["atom_edges"].shape[0])
    res["num_probe_edges"] = torch.tensor(res["probe_edges"].shape[0])
    res["num_probes"] = torch.tensor(res["probe_target"].shape[0])

    return res

def atoms_to_graph_dict(atoms, cutoff):
    probe_pos = np.zeros((0,3))
    atom_edges, atom_edges_features, _ = atoms_to_graph(atoms, cutoff)

    default_type = torch.get_default_dtype()

    # pylint: disable=E1102
    res = {
        "nodes": torch.tensor(atoms.get_atomic_numbers()),
        "atom_edges": torch.tensor(np.concatenate(atom_edges, axis=0)),
        "atom_edges_features": torch.tensor(
            np.concatenate(atom_edges_features, axis=0)[:, None], dtype=default_type
        ),
    }
    res["num_nodes"] = torch.tensor(res["nodes"].shape[0])
    res["num_atom_edges"] = torch.tensor(res["atom_edges"].shape[0])

    return res

def atoms_to_graph(atoms, cutoff):
    atom_edges = []
    atom_edges_features = []

    # Compute neighborlist
    if np.any(atoms.get_cell().lengths() <= 0.0001) or (np.any(atoms.get_pbc()) and np.any(atoms.get_cell().lengths() < cutoff)):
        neighborlist = AseNeigborListWrapper(cutoff, atoms)
    else:
        neighborlist = asap3.FullNeighborList(cutoff, atoms)
    atomic_numbers = atoms.get_atomic_numbers()
    for i in range(len(atoms)):
        neigh_idx, _, neigh_dist2 = neighborlist.get_neighbors(i, cutoff)
        neigh_dist = np.sqrt(neigh_dist2)
        neigh_atomic_species = atomic_numbers[neigh_idx]

        self_index = np.ones_like(neigh_idx) * i
        edges = np.stack((neigh_idx, self_index), axis=1)

        atom_edges.append(edges)
        atom_edges_features.append(neigh_dist)

    return atom_edges, atom_edges_features, neighborlist

def probes_to_graph(atoms, probe_pos, cutoff, neighborlist=None):
    probe_edges = []
    probe_edges_features = []
    if hasattr(neighborlist, "get_neighbors_querypoint"):
        results = neighborlist.get_neighbors_querypoint(probe_pos)
        atomic_numbers = atoms.get_atomic_numbers()
    else:
        # Insert probe atoms
        num_probes = probe_pos.shape[0]
        probe_atoms = ase.Atoms(numbers=[0] * num_probes, positions=probe_pos)
        atoms_with_probes = atoms.copy()
        atoms_with_probes.extend(probe_atoms)
        atomic_numbers = atoms_with_probes.get_atomic_numbers()

        probe_edges = []
        probe_edges_features = []

        if np.any(atoms.get_cell().lengths() <= 0.0001) or (np.any(atoms.get_pbc()) and np.any(atoms.get_cell().lengths() < cutoff)):
            neighborlist = AseNeigborListWrapper(cutoff, atoms_with_probes)
        else:
            neighborlist = asap3.FullNeighborList(cutoff, atoms_with_probes)

        results = [neighborlist.get_neighbors(i+len(atoms), cutoff) for i in range(num_probes)]

    for i, (neigh_idx, neigh_diff, neigh_dist2) in enumerate(results):
        neigh_dist = np.sqrt(neigh_dist2)
        neigh_atomic_species = atomic_numbers[neigh_idx]

        neigh_is_atom = neigh_atomic_species != 0
        neigh_atoms = neigh_idx[neigh_is_atom]
        self_index = np.ones_like(neigh_atoms) * i
        edges = np.stack((neigh_atoms, self_index), axis=1)
        probe_edges.append(edges)
        probe_edges_features.append(neigh_dist[neigh_is_atom])

    return probe_edges, probe_edges_features

def collate_list_of_dicts(list_of_dicts, pin_memory=False):
    # Convert from "list of dicts" to "dict of lists"
    dict_of_lists = {k: [dic[k] for dic in list_of_dicts] for k in list_of_dicts[0]}

    # Convert each list of tensors to single tensor with pad and stack
    if pin_memory:
        pin = lambda x: x.pin_memory()
    else:
        pin = lambda x: x

    collated = {k: pin(pad_and_stack(dict_of_lists[k])) for k in dict_of_lists}
    return collated

class CollateFuncRandomSample:
    def __init__(self, cutoff, num_probes, pin_memory=True, disable_pbc=False):
        self.num_probes = num_probes
        self.cutoff = cutoff
        self.pin_memory = pin_memory
        self.disable_pbc = disable_pbc

    def __call__(self, input_dicts: List):
        graphs = []
        for i in input_dicts:
            if self.disable_pbc:
                atoms = i["atoms"].copy()
                atoms.set_pbc(False)
            else:
                atoms = i["atoms"]

            graphs.append(atoms_and_probe_sample_to_graph_dict(
                i["density"],
                atoms,
                i["grid_position"],
                self.cutoff,
                self.num_probes,
            ))

        return collate_list_of_dicts(graphs, pin_memory=self.pin_memory)

class CollateFuncAtoms:
    def __init__(self, cutoff, pin_memory=True, disable_pbc=False):
        self.cutoff = cutoff
        self.pin_memory = pin_memory
        self.disable_pbc = disable_pbc

    def __call__(self, input_dicts: List):
        graphs = []
        for i in input_dicts:
            if self.disable_pbc:
                atoms = i["atoms"].copy()
                atoms.set_pbc(False)
            else:
                atoms = i["atoms"]

            graphs.append(atoms_to_graph_dict(
                atoms,
                self.cutoff,
            ))

        return collate_list_of_dicts(graphs, pin_memory=self.pin_memory)


def _calculate_grid_pos(density, origin, cell):
    # Calculate grid positions
    ngridpts = np.array(density.shape)  # grid matrix
    grid_pos = np.meshgrid(
        np.arange(ngridpts[0]) / density.shape[0],
        np.arange(ngridpts[1]) / density.shape[1],
        np.arange(ngridpts[2]) / density.shape[2],
        indexing="ij",
    )
    grid_pos = np.stack(grid_pos, 3)
    grid_pos = np.dot(grid_pos, cell)
    grid_pos = grid_pos + origin
    return grid_pos


def _decompress(tar, tarinfo):
    """Extract compressed tar file member and return a bytes object with the content"""

    bytesobj = tar.extractfile(tarinfo).read()
    if tarinfo.name.endswith(".zz"):
        filecontent = zlib.decompress(bytesobj)
    elif tarinfo.name.endswith(".lz4"):
        filecontent = lz4.frame.decompress(bytesobj)
    else:
        filecontent = bytesobj

    return filecontent

def _read_vasp(filecontent):
    # Write to tmp file and read using ASE
    tmpfd, tmppath = tempfile.mkstemp(prefix="tmpdeepdft")
    tmpfile = os.fdopen(tmpfd, "wb")
    tmpfile.write(filecontent)
    tmpfile.close()
    vasp_charge = VaspChargeDensity(filename=tmppath)
    os.remove(tmppath)
    density = vasp_charge.chg[-1]  # separate density
    atoms = vasp_charge.atoms[-1]  # separate atom positions

    return density, atoms, np.zeros(3)  # TODO: Can we always assume origin at 0,0,0?


def _read_cube(filecontent):
    textbuf = io.StringIO(filecontent.decode())
    cube = ase.io.cube.read_cube(textbuf)
    # sometimes there is an entry at index 3
    # denoting the number of values for each grid position
    origin = cube["origin"][0:3]
    # by convention the cube electron density is given in electrons/Bohr^3,
    # and ase read_cube does not convert to electrons/Å^3, so we do the conversion here
    cube["data"] *= 1.0 / ase.units.Bohr ** 3
    return cube["data"], cube["atoms"], origin
