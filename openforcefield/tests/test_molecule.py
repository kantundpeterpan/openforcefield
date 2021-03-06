#!/usr/bin/env python

#=============================================================================================
# MODULE DOCSTRING
#=============================================================================================

"""
Tests for molecular topology representations

At least one supported cheminformatics toolkit must be installed to run these tests.
Only the tests applicable to that toolkit will be run.

TODO:
- Add tests comparing RDKit and OpenEye aromaticity perception
- Right now, the test database of TestMolecule is read from mol2, requiring the OE
  toolkit. Find a different test set that RDKit can read, or make a database of
  serialized OFFMols.

"""

#=============================================================================================
# GLOBAL IMPORTS
#=============================================================================================

import copy
import os
import pickle
from tempfile import NamedTemporaryFile

import numpy as np
import pytest
from simtk import unit

from openforcefield.topology.molecule import Molecule, Atom, InvalidConformerError
from openforcefield.utils import get_data_file_path
# TODO: Will the ToolkitWrapper allow us to pare that down?
from openforcefield.utils.toolkits import OpenEyeToolkitWrapper, RDKitToolkitWrapper, AmberToolsToolkitWrapper, ToolkitRegistry
from openforcefield.tests.test_forcefield import create_ethanol, create_reversed_ethanol, create_acetaldehyde, \
    create_benzene_no_aromatic, create_cyclohexane

#=============================================================================================
# TEST UTILITIES
#=============================================================================================

requires_openeye = pytest.mark.skipif(not OpenEyeToolkitWrapper.is_available(),
                                      reason='Test requires OE toolkit')
requires_rdkit = pytest.mark.skipif(not RDKitToolkitWrapper.is_available(),
                                    reason='Test requires RDKit')


def assert_molecule_is_equal(molecule1, molecule2, msg):
    """Compare whether two Molecule objects are equal

    Parameters
    ----------
    molecule1, molecule2 : openforcefield.topology.Molecule
        Molecules to be compared
    msg : str
        Message to include if molecules fail to match.

    """
    if not(molecule1.is_isomorphic_with(molecule2)):
        raise AssertionError(msg)


def is_four_memebered_ring_torsion(torsion):
    """Check that three atoms in the given torsion form a four-membered ring."""
    # Push a copy of the first and second atom in the end to make the code simpler.
    torsion = list(torsion) + [torsion[0], torsion[1]]

    is_four_membered_ring = True
    for i in range(4):
        # The atom is bonded to the next one.
        is_four_membered_ring &= torsion[i].is_bonded_to(torsion[i+1])
        # The atom is not bonded to the atom on its diagonal.
        is_four_membered_ring &= not torsion[i].is_bonded_to(torsion[i+2])

    return is_four_membered_ring


def is_three_memebered_ring_torsion(torsion):
    """Check that three atoms in the given torsion form a three-membered ring.

    In order to be 4 atoms with a three-membered ring, there must be
    1) A central atom connected to all other atoms.
    2) An atom outside the ring connected exclusively to the central atom.
    3) Two atoms in the ring connected to the central atom and to each other.

    """
    # A set of atom indices for the atoms in the torsion.
    torsion_atom_indices = set(a.molecule_atom_index for a in torsion)

    # Collect all the bonds involving exclusively atoms in the torsion.
    bonds_by_atom_idx = {i: set() for i in torsion_atom_indices}
    for atom in torsion:
        for bond in atom.bonds:
            # Consider the bond only if both atoms are in the torsion.
            if (bond.atom1_index in torsion_atom_indices and
                        bond.atom2_index in torsion_atom_indices):
                bonds_by_atom_idx[bond.atom1_index].add(bond.atom2_index)
                bonds_by_atom_idx[bond.atom2_index].add(bond.atom1_index)

    # Find the central atom, which is connected to all other atoms.
    atom_indices = [i for i in torsion_atom_indices if len(bonds_by_atom_idx[i]) == 3]
    if len(atom_indices) != 1:
        return False
    central_atom_idx = atom_indices[0]

    # Find the atom outside the ring.
    atom_indices = [i for i in torsion_atom_indices if len(bonds_by_atom_idx[i]) == 1]
    if len(atom_indices) != 1 or central_atom_idx not in bonds_by_atom_idx[atom_indices[0]]:
        return False
    outside_atom_idx = atom_indices[0]

    # Check that the remaining two atoms are non-central atoms in the membered ring.
    atom1, atom2 = [i for i in torsion_atom_indices if i not in [central_atom_idx, outside_atom_idx]]
    # The two atoms are bonded to each other.
    if atom2 not in bonds_by_atom_idx[atom1] or atom1 not in bonds_by_atom_idx[atom2]:
        return False
    # Check that they are both bonded to the central atom and none other.
    for atom_idx in [atom1, atom2]:
        if (central_atom_idx not in bonds_by_atom_idx[atom_idx] or
                    len(bonds_by_atom_idx[atom_idx]) != 2):
            return False

    # This is a torsion including a three-membered ring.
    return True


#=============================================================================================
# FIXTURES
#=============================================================================================

def mini_drug_bank(xfail_mols=None, wip_mols=None):
    """Load the full MiniDrugBank into Molecule objects.

    Parameters
    ----------
    xfail_mols : Dict[str, str or None]
        Dictionary mapping the molecule names that are allowed to
        failed to the failure reason.
    wip_mols : Dict[str, str or None]
        Dictionary mapping the molecule names that are work in progress
        to the failure reason.

    """
    # If we have already loaded the data set, return the cached one.
    if mini_drug_bank.molecules is not None:
        molecules = mini_drug_bank.molecules
    else:
        # Load the dataset.
        file_path = get_data_file_path('molecules/MiniDrugBank_tripos.mol2')
        try:
            # We need OpenEye to parse the molecules, but pytest execute this
            # whether or not the test class is skipped so if OE is not available
            # we just return an empty list of test cases as a workaround.
            molecules = Molecule.from_file(file_path, allow_undefined_stereo=True)
        except NotImplementedError as e:
            assert 'No toolkits in registry can read file' in str(e)
            mini_drug_bank.molecules = []
            return []
        else:
            mini_drug_bank.molecules = molecules

    # Check if we need to mark anything.
    if xfail_mols is None and wip_mols is None:
        return molecules

    # Handle mutable default.
    if xfail_mols is None:
        xfail_mols = {}
    if wip_mols is None:
        wip_mols = {}
    # There should be no molecule in both dictionaries.
    assert len(set(xfail_mols).intersection(set(wip_mols))) == 0

    # Don't modify the cached molecules.
    molecules = copy.deepcopy(molecules)
    for i, mol in enumerate(molecules):
        if mol.name in xfail_mols:
            marker = pytest.mark.xfail(reason=xfail_mols[mol.name])
        elif mol.name in wip_mols:
            marker = pytest.mark.wip(reason=wip_mols[mol.name])
        else:
            marker = None

        if marker is not None:
            molecules[i] = pytest.param(mol, marks=marker)

    return molecules

# Use a "static" variable as a workaround as fixtures cannot be
# used inside pytest.mark.parametrize (see issue #349 in pytest).
mini_drug_bank.molecules = None

# All the molecules that raise UndefinedStereochemistryError when read by OETK()
openeye_drugbank_undefined_stereo_mols = {'DrugBank_1634', 'DrugBank_1700', 'DrugBank_1962',
                                          'DrugBank_2519', 'DrugBank_2987', 'DrugBank_3502',
                                          'DrugBank_3930', 'DrugBank_4161', 'DrugBank_4162',
                                          'DrugBank_5043', 'DrugBank_5418', 'DrugBank_6531'}

# All the molecules that raise UndefinedStereochemistryError when read by OETK().
# Note that this list is different from that for OEMol,
# since the toolkits have different definitions of "stereogenic"
rdkit_drugbank_undefined_stereo_mols = {'DrugBank_1634', 'DrugBank_1962', 'DrugBank_2519',
                                        'DrugBank_3930', 'DrugBank_5043', 'DrugBank_5418'}


# Missing stereo in OE but not RDK:  'DrugBank_2987', 'DrugBank_3502', 'DrugBank_4161',
# 'DrugBank_4162', 'DrugBank_6531', 'DrugBank_1700',

# Some molecules are _valid_ in both OETK and RDKit, but will fail if you try
# to convert from one to the other, since OE adds stereo that RDKit doesn't
drugbank_stereogenic_in_oe_but_not_rdkit = {'DrugBank_1598', 'DrugBank_4346', 'DrugBank_1849',
                                            'DrugBank_2141'}

#=============================================================================================
# TESTS
#=============================================================================================

class TestAtom:
    """Test Atom class."""

    def test_atom_constructor(self):
        """Test Atom creation"""
        # Create a non-aromatic carbon atom
        atom1 = Atom(6, 0, False)
        assert atom1.atomic_number == 6
        assert atom1.formal_charge == 0

        # Create a chiral carbon atom
        atom2 = Atom(6, 0, False, stereochemistry='R', name='CT')
        assert atom1.stereochemistry != atom2.stereochemistry

    def test_atom_properties(self):
        """Test that atom properties are correctly populated and gettable"""
        from simtk.openmm.app import element
        formal_charge = 0
        is_aromatic = False
        # Attempt to create all elements supported by OpenMM
        elements = [getattr(element, name) for name in dir(element) if (type(getattr(element, name)) == element.Element)]
        # The above runs into a problem with deuterium (fails name assertion)
        elements.remove(element.deuterium)
        for this_element in elements:
            atom = Atom(this_element.atomic_number, formal_charge, is_aromatic, name=this_element.name)
            assert atom.atomic_number == this_element.atomic_number
            assert atom.element == this_element
            assert atom.mass == this_element.mass
            assert atom.formal_charge == formal_charge
            assert atom.is_aromatic == is_aromatic
            assert atom.name == this_element.name


@requires_openeye
class TestMolecule:
    """Test Molecule class."""

    # TODO: Test getstate/setstate
    # TODO: Test {to_from}_{dict|yaml|toml|json|bson|messagepack|pickle}

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_pickle_serialization(self, molecule):
        """Test pickling of a molecule object."""
        serialized = pickle.dumps(molecule)
        molecule_copy = pickle.loads(serialized)
        assert molecule == molecule_copy

    # ----------------------------------------------------
    # Test Molecule constructors and conversion utilities.
    # ----------------------------------------------------

    def test_create_empty(self):
        """Test empty constructor."""
        molecule = Molecule()
        assert len(molecule.atoms) == 0
        assert len(molecule.bonds) == 0

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_create_copy(self, molecule):
        """Test copy constructor."""
        molecule_copy = Molecule(molecule)
        assert molecule_copy == molecule

    @pytest.mark.parametrize('toolkit', [OpenEyeToolkitWrapper, RDKitToolkitWrapper])
    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_to_from_smiles(self, molecule, toolkit):
        """Test round-trip creation from SMILES"""
        if not toolkit.is_available():
            pytest.skip('Required toolkit is unavailable')

        if toolkit == RDKitToolkitWrapper:
            # Skip the test if OpenEye assigns stereochemistry but RDKit doesn't (since then, the
            # OFF molecule will be loaded, but fail to convert in to_rdkit)
            if molecule.name in drugbank_stereogenic_in_oe_but_not_rdkit:
                pytest.skip('Molecle is stereogenic in OpenEye (which loaded this dataset), but not RDKit, so it '
                            'is impossible to make a valid RDMol in this test')
            undefined_stereo_mols = rdkit_drugbank_undefined_stereo_mols
        elif toolkit == OpenEyeToolkitWrapper:
            undefined_stereo_mols = openeye_drugbank_undefined_stereo_mols

        toolkit_wrapper = toolkit()

        undefined_stereo = molecule.name in undefined_stereo_mols

        smiles1 = molecule.to_smiles(toolkit_registry=toolkit_wrapper)
        if undefined_stereo:
            molecule2 = Molecule.from_smiles(smiles1,
                                             allow_undefined_stereo=True,
                                             toolkit_registry=toolkit_wrapper)
        else:
            molecule2 = Molecule.from_smiles(smiles1,
                                             toolkit_registry=toolkit_wrapper)
        smiles2 = molecule2.to_smiles(toolkit_registry=toolkit_wrapper)
        assert (smiles1 == smiles2)

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_unique_atom_names(self, molecule):
        """Test molecules have unique atom names"""
        # The dataset we load in has atom names, so let's strip them first
        # to ensure that we can fail the uniqueness check
        for atom in molecule.atoms:
            atom.name = ''
        assert not(molecule.has_unique_atom_names)
        # Then genreate unique atom names using the built in algorithm
        molecule.generate_unique_atom_names()
        # Check that the molecule has unique atom names
        assert molecule.has_unique_atom_names
        # Check molecule.has_unique_atom_names is working correctly
        assert ((len(set([atom.name for atom in molecule.atoms])) == molecule.n_atoms) == molecule.has_unique_atom_names)
        molecule.atoms[1].name = molecule.atoms[0].name # no longer unique
        assert ((len(set([atom.name for atom in molecule.atoms])) == molecule.n_atoms) == molecule.has_unique_atom_names)

    # TODO: Should there be an equivalent toolkit test and leave this as an integration test?
    @pytest.mark.slow
    def test_create_from_file(self):
        """Test standard constructor taking a filename or file-like object."""
        # TODO: Expand test to both openeye and rdkit toolkits
        filename = get_data_file_path('molecules/toluene.mol2')

        molecule1 = Molecule(filename, allow_undefined_stereo=True)
        with open(filename, 'r') as infile:
            molecule2 = Molecule(infile, file_format='MOL2', allow_undefined_stereo=True)
        assert molecule1 == molecule2

        import gzip
        with gzip.GzipFile(filename + '.gz', 'r') as infile:
            molecule3 = Molecule(infile, file_format='MOL2', allow_undefined_stereo=True)
        assert molecule3 == molecule1

        # Ensure that attempting to initialize a single Molecule from a file
        # containing multiple molecules raises a ValueError
        with pytest.raises(ValueError) as exc_info:
            filename = get_data_file_path('molecules/zinc-subset-tripos.mol2.gz')
            molecule = Molecule(filename, allow_undefined_stereo=True)

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_create_from_serialized(self, molecule):
        """Test standard constructor taking the output of __getstate__()."""
        serialized_molecule = molecule.__getstate__()
        molecule_copy = Molecule(serialized_molecule)
        assert molecule == molecule_copy

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_to_from_dict(self, molecule):
        """Test that conversion/creation of a molecule to and from a dict is consistent."""
        serialized = molecule.to_dict()
        molecule_copy = Molecule.from_dict(serialized)
        assert molecule == molecule_copy

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_to_networkx(self, molecule):
        """Test conversion to NetworkX graph."""
        graph = molecule.to_networkx()

    @requires_rdkit
    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_to_from_rdkit(self, molecule):
        """Test that conversion/creation of a molecule to and from an RDKit rdmol is consistent.
        """
        # import pickle
        from openforcefield.utils.toolkits import UndefinedStereochemistryError

        undefined_stereo = molecule.name in rdkit_drugbank_undefined_stereo_mols

        toolkit_wrapper = RDKitToolkitWrapper()

        rdmol = molecule.to_rdkit()
        molecule_smiles = molecule.to_smiles(toolkit_registry=toolkit_wrapper)

        # First test making a molecule using the Molecule(oemol) method

        # If this is a known failure, check that it raises UndefinedStereochemistryError
        # and proceed with the test ignoring it.
        test_mol =  None
        if undefined_stereo:
            with pytest.raises(UndefinedStereochemistryError):
                Molecule(rdmol)
            test_mol = Molecule(rdmol, allow_undefined_stereo=True)
        else:
            test_mol = Molecule(rdmol)

        test_mol_smiles = test_mol.to_smiles(toolkit_registry=toolkit_wrapper)
        assert molecule_smiles == test_mol_smiles

        # Check that the two topologies are isomorphic.
        assert_molecule_is_equal(molecule, test_mol, 'Molecule.to_rdkit()/Molecule(rdmol) round trip failed')

        # Second, test making a molecule using the Molecule.from_openeye(oemol) method

        # If this is a known failure, check that it raises UndefinedStereochemistryError
        # and proceed with the test.
        if undefined_stereo:
            with pytest.raises(UndefinedStereochemistryError):
                Molecule.from_rdkit(rdmol)
            test_mol = Molecule.from_rdkit(rdmol, allow_undefined_stereo=True)
        else:
            test_mol = Molecule.from_rdkit(rdmol)

        test_mol_smiles = test_mol.to_smiles(toolkit_registry=toolkit_wrapper)
        assert molecule_smiles == test_mol_smiles

        # Check that the two topologies are isomorphic.
        assert_molecule_is_equal(molecule, test_mol, 'Molecule.to_rdkit()/from_rdkit() round trip failed')


    # TODO: Should there be an equivalent toolkit test and leave this as an integration test?
    @requires_openeye
    @pytest.mark.parametrize('molecule', mini_drug_bank(
        xfail_mols={
            'DrugBank_2397': 'OpenEye cannot generate a correct IUPAC name and raises a "Warning: Incorrect name:" or simply return "BLAH".',
            'DrugBank_2543': 'OpenEye cannot generate a correct IUPAC name and raises a "Warning: Incorrect name:" or simply return "BLAH".',
            'DrugBank_2642': 'OpenEye cannot generate a correct IUPAC name and raises a "Warning: Incorrect name:" or simply return "BLAH".',
        },
        wip_mols={
            'DrugBank_1212': 'the roundtrip generates molecules with very different IUPAC/SMILES!',
            'DrugBank_2210': 'the roundtrip generates molecules with very different IUPAC/SMILES!',
            'DrugBank_4584': 'the roundtrip generates molecules with very different IUPAC/SMILES!',

            'DrugBank_390': 'raises warning "Unable to make OFFMol from OEMol: OEMol has unspecified stereochemistry."',
            'DrugBank_810': 'raises warning "Unable to make OFFMol from OEMol: OEMol has unspecified stereochemistry."',
            'DrugBank_4316': 'raises warning "Unable to make OFFMol from OEMol: OEMol has unspecified stereochemistry."',
            'DrugBank_7124': 'raises warning "Unable to make OFFMol from OEMol: OEMol has unspecified stereochemistry."',

            'DrugBank_4346': 'raises warning "Failed to parse name:"',
        }
    ))
    def test_to_from_iupac(self, molecule):
        """Test that conversion/creation of a molecule to and from a IUPAC name is consistent."""
        from openforcefield.utils.toolkits import UndefinedStereochemistryError

        # All the molecules that raise UndefinedStereochemistryError in Molecule.from_iupac()
        # (This is a larger list than the normal group of undefined stereo mols, probably has
        # something to do with IUPAC information content)
        iupac_problem_mols = {'DrugBank_977', 'DrugBank_1634', 'DrugBank_1700', 'DrugBank_1962',
                              'DrugBank_2148', 'DrugBank_2178', 'DrugBank_2186', 'DrugBank_2208',
                              'DrugBank_2519', 'DrugBank_2538', 'DrugBank_2592', 'DrugBank_2651',
                              'DrugBank_2987', 'DrugBank_3332', 'DrugBank_3502', 'DrugBank_3622',
                              'DrugBank_3726', 'DrugBank_3844', 'DrugBank_3930', 'DrugBank_4161',
                              'DrugBank_4162', 'DrugBank_4778', 'DrugBank_4593', 'DrugBank_4959',
                              'DrugBank_5043', 'DrugBank_5076', 'DrugBank_5176', 'DrugBank_5418',
                              'DrugBank_5737', 'DrugBank_5902', 'DrugBank_6304', 'DrugBank_6305',
                              'DrugBank_6329', 'DrugBank_6355', 'DrugBank_6401', 'DrugBank_6509',
                              'DrugBank_6531', 'DrugBank_6647',

                              # These test cases are allowed to fail.
                              'DrugBank_390', 'DrugBank_810', 'DrugBank_4316', 'DrugBank_4346',
                              'DrugBank_7124'
                              }
        undefined_stereo = molecule.name in iupac_problem_mols

        iupac = molecule.to_iupac()

        if undefined_stereo:
            with pytest.raises(UndefinedStereochemistryError):
                Molecule.from_iupac(iupac)

        molecule_copy = Molecule.from_iupac(iupac, allow_undefined_stereo=undefined_stereo)
        assert molecule.is_isomorphic_with(molecule_copy,
                                           atom_stereochemistry_matching=not undefined_stereo)

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_to_from_topology(self, molecule):
        """Test that conversion/creation of a molecule to and from a Topology is consistent."""
        topology = molecule.to_topology()
        molecule_copy = Molecule.from_topology(topology)
        assert molecule == molecule_copy

    # TODO: Should there be an equivalent toolkit test and leave this as an integration test?
    @pytest.mark.parametrize('molecule', mini_drug_bank())
    @pytest.mark.parametrize('format', [
        'mol2',
        'sdf',
        pytest.param('pdb', marks=pytest.mark.wip(reason='Read from pdb has not been implemented properly yet'))
    ])
    def test_to_from_file(self, molecule, format):
        """Test that conversion/creation of a molecule to and from a file is consistent."""
        from openforcefield.utils.toolkits import UndefinedStereochemistryError
        # TODO: Test all file capabilities; the current test is minimal

        # TODO: This is only for OE. Expand to both OE and RDKit toolkits.
        # Molecules that are known to raise UndefinedStereochemistryError.
        undefined_stereo_mols = {'DrugBank_1700', 'DrugBank_2987', 'DrugBank_3502', 'DrugBank_4161',
                                 'DrugBank_4162', 'DrugBank_6531'}
        undefined_stereo = molecule.name in undefined_stereo_mols

        # The file is automatically deleted outside the with-clause.
        with NamedTemporaryFile(suffix='.' + format) as iofile:
            # If this has undefined stereo, check that the exception is raised.
            extension = os.path.splitext(iofile.name)[1][1:]
            molecule.to_file(iofile.name, extension)
            if undefined_stereo:
                with pytest.raises(UndefinedStereochemistryError):
                    Molecule.from_file(iofile.name)
            molecule2 = Molecule.from_file(iofile.name, allow_undefined_stereo=undefined_stereo)
            assert molecule == molecule2
            # TODO: Test to make sure properties are preserved?
            # NOTE: We can't read pdb files and expect chemical information to be preserved

    @requires_openeye
    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_to_from_oemol(self, molecule):
        """Test that conversion/creation of a molecule to and from a OEMol is consistent."""
        from openforcefield.utils.toolkits import UndefinedStereochemistryError

        # Known failures raise an UndefinedStereochemistryError, but
        # the round-trip SMILES representation with the OpenEyeToolkit
        # doesn't seem to be affected.

        # ZINC test set known failures.
        # known_failures = {'ZINC05964684', 'ZINC05885163', 'ZINC05543156', 'ZINC17211981',
        #                   'ZINC17312986', 'ZINC06424847', 'ZINC04963126'}

        undefined_stereo = molecule.name in openeye_drugbank_undefined_stereo_mols

        toolkit_wrapper = OpenEyeToolkitWrapper()

        oemol = molecule.to_openeye()
        molecule_smiles = molecule.to_smiles(toolkit_registry=toolkit_wrapper)

        # First test making a molecule using the Molecule(oemol) method

        # If this is a known failure, check that it raises UndefinedStereochemistryError
        # and proceed with the test ignoring it.
        test_mol =  None
        if undefined_stereo:
            with pytest.raises(UndefinedStereochemistryError):
                Molecule(oemol)
            test_mol = Molecule(oemol, allow_undefined_stereo=True)
        else:
            test_mol = Molecule(oemol)

        test_mol_smiles = test_mol.to_smiles(toolkit_registry=toolkit_wrapper)
        assert molecule_smiles == test_mol_smiles

        # Check that the two topologies are isomorphic.
        assert_molecule_is_equal(molecule, test_mol, 'Molecule.to_openeye()/Molecule(oemol) round trip failed')

        # Second, test making a molecule using the Molecule.from_openeye(oemol) method

        # If this is a known failure, check that it raises UndefinedStereochemistryError
        # and proceed with the test.
        if undefined_stereo:
            with pytest.raises(UndefinedStereochemistryError):
                Molecule.from_openeye(oemol)
            test_mol = Molecule.from_openeye(oemol, allow_undefined_stereo=True)
        else:
            test_mol = Molecule.from_openeye(oemol)

        test_mol_smiles = test_mol.to_smiles(toolkit_registry=toolkit_wrapper)
        assert molecule_smiles == test_mol_smiles

        # Check that the two topologies are isomorphic.
        assert_molecule_is_equal(molecule, test_mol, 'Molecule.to_openeye()/from_openeye() round trip failed')


    # ----------------------------------------------------
    # Test properties.
    # ----------------------------------------------------

    def test_name(self):
        """Test Molecule name property"""
        molecule1 = Molecule()
        molecule1.name = None

        molecule2 = Molecule()
        molecule2.name = ''
        assert molecule1.name == molecule2.name

        name = 'benzene'
        molecule = Molecule()
        molecule.name = name
        assert molecule.name == name

    def test_hill_formula(self):
        """Test that making the hill formula is consistent between input methods and ordering"""
        # make sure smiles match reference
        molecule_smiles = create_ethanol()
        assert molecule_smiles.hill_formula == 'C2H6O'
        # make sure is not order dependent
        molecule_smiles_reverse = create_reversed_ethanol()
        assert molecule_smiles.hill_formula == molecule_smiles_reverse.hill_formula
        # make sure single element names are put first
        order_mol = Molecule.from_smiles('C(Br)CB')
        assert order_mol.hill_formula == 'C2H6BBr'
        # test molecule with no carbon
        no_carb_mol = Molecule.from_smiles('OS(=O)(=O)O')
        assert no_carb_mol.hill_formula == 'H2O4S'
        # test no carbon and hydrogen
        br_i = Molecule.from_smiles('BrI')
        assert br_i.hill_formula == 'BrI'
        # make sure files and smiles match
        molecule_file = Molecule.from_file(get_data_file_path('molecules/ethanol.sdf'))
        assert molecule_smiles.hill_formula == molecule_file.hill_formula
        # make sure the topology molecule gives the same formula
        from openforcefield.topology.topology import TopologyMolecule, Topology
        topology = Topology.from_molecules(molecule_smiles)
        topmol = TopologyMolecule(molecule_smiles, topology)
        assert molecule_smiles.hill_formula == Molecule.to_hill_formula(topmol)
        # make sure the networkx matches
        assert molecule_smiles.hill_formula == Molecule.to_hill_formula(molecule_smiles.to_networkx())


    def test_isomorphic_general(self):
        """Test the matching using different input types"""
        # check that hill formula fails are caught
        ethanol = create_ethanol()
        acetaldehyde = create_acetaldehyde()
        assert ethanol.is_isomorphic_with(acetaldehyde) is False
        assert acetaldehyde.is_isomorphic_with(ethanol) is False
        # check that different orderings work with full matching
        ethanol_reverse = create_reversed_ethanol()
        assert ethanol.is_isomorphic_with(ethanol_reverse) is True
        # check a reference mapping between ethanol and ethanol_reverse matches that calculated
        ref_mapping = {0: 8, 1: 7, 2: 6, 3: 3, 4: 4, 5: 5, 6: 1, 7: 2, 8: 0}
        assert Molecule.are_isomorphic(ethanol, ethanol_reverse, return_atom_map=True)[1] == ref_mapping
        # check matching with nx.Graph atomic numbers and connectivity only
        assert Molecule.are_isomorphic(ethanol, ethanol_reverse.to_networkx(), aromatic_matching=False,
                                       formal_charge_matching=False, bond_order_matching=False,
                                       atom_stereochemistry_matching=False,
                                       bond_stereochemistry_matching=False)[0] is True
        # check matching with nx.Graph with full matching
        assert ethanol.is_isomorphic_with(ethanol_reverse.to_networkx()) is True
        # check matching with a TopologyMolecule class
        from openforcefield.topology.topology import TopologyMolecule, Topology
        topology = Topology.from_molecules(ethanol)
        topmol = TopologyMolecule(ethanol, topology)
        assert Molecule.are_isomorphic(ethanol, topmol, aromatic_matching=False, formal_charge_matching=False,
                                       bond_order_matching=False, atom_stereochemistry_matching=False,
                                       bond_stereochemistry_matching=False)[0] is True
        # test hill formula passes but isomorphic fails
        mol1 = Molecule.from_smiles('Fc1ccc(F)cc1')
        mol2 = Molecule.from_smiles('Fc1ccccc1F')
        assert mol1.is_isomorphic_with(mol2) is False
        assert mol2.is_isomorphic_with(mol1) is False

    isomorphic_permutations = [{'aromatic_matching': True, 'formal_charge_matching': True, 'bond_order_matching': True,
                                'atom_stereochemistry_matching': True, 'bond_stereochemistry_matching': True,
                                'result': False},
                               {'aromatic_matching': False, 'formal_charge_matching': True, 'bond_order_matching': True,
                                'atom_stereochemistry_matching': True, 'bond_stereochemistry_matching': True,
                                'result': False},
                               {'aromatic_matching': True, 'formal_charge_matching': False, 'bond_order_matching': True,
                                'atom_stereochemistry_matching': True, 'bond_stereochemistry_matching': True,
                                'result': False},
                               {'aromatic_matching': True, 'formal_charge_matching': True, 'bond_order_matching': False,
                                'atom_stereochemistry_matching': True, 'bond_stereochemistry_matching': True,
                                'result': False},
                               {'aromatic_matching': True, 'formal_charge_matching': True, 'bond_order_matching': True,
                                'atom_stereochemistry_matching': False, 'bond_stereochemistry_matching': True,
                                'result': False},
                               {'aromatic_matching': True, 'formal_charge_matching': True, 'bond_order_matching': True,
                                'atom_stereochemistry_matching': True, 'bond_stereochemistry_matching': False,
                                'result': False},
                               {'aromatic_matching': False, 'formal_charge_matching': False, 'bond_order_matching': False,
                                'atom_stereochemistry_matching': False, 'bond_stereochemistry_matching': False,
                                'result': True},
                               {'aromatic_matching': False, 'formal_charge_matching': True, 'bond_order_matching': False,
                                'atom_stereochemistry_matching': True, 'bond_stereochemistry_matching': True,
                                'result': True},
                               {'aromatic_matching': False, 'formal_charge_matching': False, 'bond_order_matching': False,
                                'atom_stereochemistry_matching': True, 'bond_stereochemistry_matching': True,
                                'result': True},
                               ]

    @pytest.mark.parametrize('inputs', isomorphic_permutations)
    def test_isomorphic_perumtations(self, inputs):
        """Test all of the different combinations of matching levels between benzene with and without the aromatic bonds
        defined"""
        # get benzene with all aromatic atoms/bonds labeled
        benzene = Molecule.from_smiles('c1ccccc1')
        # get benzene with no aromatic labels
        benzene_no_aromatic = create_benzene_no_aromatic()
        # now test all of the variations
        assert Molecule.are_isomorphic(benzene, benzene_no_aromatic, aromatic_matching=inputs['aromatic_matching'],
                                       formal_charge_matching=inputs['formal_charge_matching'],
                                       bond_order_matching=inputs['bond_order_matching'],
                                       atom_stereochemistry_matching=inputs['atom_stereochemistry_matching'],
                                       bond_stereochemistry_matching=inputs['bond_stereochemistry_matching'])[0] is inputs['result']

    def test_remap(self):
        """Test the remap function which should return a new molecule in the requested ordering"""
        # the order here is CCO
        ethanol = create_ethanol()
        # get ethanol in reverse order OCC
        ethanol_reverse = create_reversed_ethanol()
        # get the mapping between the molecules
        mapping = Molecule.are_isomorphic(ethanol, ethanol_reverse, True)[1]
        ethanol.add_bond_charge_virtual_site([0, 1], 0.3 * unit.angstrom)
        # make sure that molecules with virtual sites raises an error
        with pytest.raises(NotImplementedError):
            remapped = ethanol.remap(mapping, current_to_new=True)

        # remake with no virtual site and remap to match the reversed ordering
        ethanol = create_ethanol()

        new_ethanol = ethanol.remap(mapping, current_to_new=True)

        def assert_molecules_match_after_remap(mol1, mol2):
            """Check all of the attributes in a molecule match after being remapped"""
            for atoms in zip(mol1.atoms, mol2.atoms):
                assert atoms[0].to_dict() == atoms[1].to_dict()
            # bonds will not be in the same order in the molecule and the atom1 and atom2 indecies could be out of order
            # make a dict to compare them both
            remapped_bonds = dict(((bond.atom1_index, bond.atom2_index), bond) for bond in mol2.bonds)
            for bond in mol1.bonds:
                key = (bond.atom1_index, bond.atom2_index)
                if key not in remapped_bonds:
                    key = tuple(reversed(key))
                assert key in remapped_bonds
                # now compare each attribute of the bond except the atom indexes
                bond_dict = bond.to_dict()
                del bond_dict['atom1']
                del bond_dict['atom2']
                remapped_bond_dict = remapped_bonds[key].to_dict()
                del remapped_bond_dict['atom1']
                del remapped_bond_dict['atom2']
            assert mol1.n_bonds == mol2.n_bonds
            assert mol1.n_angles == mol2.n_angles
            assert mol1.n_propers == mol2.n_propers
            assert mol1.n_impropers == mol2.n_impropers
            assert mol1.total_charge == mol2.total_charge
            assert mol1.partial_charges.all() == mol2.partial_charges.all()

        # check all of the properties match as well, torsions and impropers will be in a different order
        # due to the bonds being out of order
        assert_molecules_match_after_remap(new_ethanol, ethanol_reverse)

        # test round trip (double remapping a molecule)
        new_ethanol = ethanol.remap(mapping, current_to_new=True)
        isomorphic, round_trip_mapping = Molecule.are_isomorphic(new_ethanol, ethanol, return_atom_map=True)
        assert isomorphic is True
        round_trip_ethanol = new_ethanol.remap(round_trip_mapping, current_to_new=True)
        assert_molecules_match_after_remap(round_trip_ethanol, ethanol)

    @requires_openeye
    def test_canonical_ordering_openeye(self):
        """Make sure molecules are returned in canonical ordering of openeye"""
        from openforcefield.utils.toolkits import OpenEyeToolkitWrapper

        openeye = OpenEyeToolkitWrapper()
        # get ethanol in canonical order
        ethanol = create_ethanol()
        # get reversed non canonical ethanol
        reversed_ethanol = create_reversed_ethanol()
        # get the canonical ordering
        canonical_ethanol = reversed_ethanol.canonical_order_atoms(openeye)
        # make sure the mapping between the ethanol and the openeye ref canonical form is the same
        assert (True, {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8}) == Molecule.are_isomorphic(canonical_ethanol,
                                                                                                         ethanol, True)

    @requires_rdkit
    def test_canonical_ordering_rdkit(self):
        """Make sure molecules are returned in canonical ordering of the RDKit"""
        from openforcefield.utils.toolkits import RDKitToolkitWrapper

        rdkit = RDKitToolkitWrapper()
        # get ethanol in canonical order
        ethanol = create_ethanol()
        # get reversed non canonical ethanol
        reversed_ethanol = create_reversed_ethanol()
        # get the canonical ordering
        canonical_ethanol = reversed_ethanol.canonical_order_atoms(rdkit)
        # make sure the mapping between the ethanol and the rdkit ref canonical form is the same
        assert (True, {0: 2, 1: 0, 2: 1, 3: 8, 4: 3, 5: 4, 6: 5, 7: 6, 8: 7}) == Molecule.are_isomorphic(canonical_ethanol,
                                                                                                         ethanol,
                                                                                                         True)

    def test_too_small_remap(self):
        """Make sure remap fails if we do not supply enough indexes"""
        ethanol = Molecule.from_file(get_data_file_path('molecules/ethanol.sdf'))
        # catch mappings that are the wrong size
        too_small_mapping = {0: 1}
        with pytest.raises(ValueError):
            new_ethanol = ethanol.remap(too_small_mapping, current_to_new=True)

    def test_wrong_index_mapping(self):
        """Make sure the remap fails when the indexing starts from the wrong value"""
        ethanol = Molecule.from_file(get_data_file_path('molecules/ethanol.sdf'))
        mapping = {0: 2, 1: 1, 2: 0, 3: 6, 4: 7, 5: 8, 6: 4, 7: 5, 8: 3}
        wrong_index_mapping = dict((i + 10, new_id) for i, new_id in enumerate(mapping.values()))
        with pytest.raises(IndexError):
            new_ethanol = ethanol.remap(wrong_index_mapping, current_to_new=True)

    @requires_rdkit
    def test_from_pdb_and_smiles(self):
        """Test the ability to make a valid molecule using RDKit and SMILES together"""
        # try and make a molecule from a pdb and smiles that don't match
        with pytest.raises(InvalidConformerError):
            mol = Molecule.from_pdb_and_smiles(get_data_file_path('molecules/toluene.pdb'), 'CC')

        # make a molecule from the toluene pdb file and the correct smiles
        mol = Molecule.from_pdb_and_smiles(get_data_file_path('molecules/toluene.pdb'), 'Cc1ccccc1')

        # make toluene from the sdf file
        mol_sdf = Molecule.from_file(get_data_file_path('molecules/toluene.sdf'))
        # get the mapping between them and compare the properties
        isomorphic, atom_map = Molecule.are_isomorphic(mol, mol_sdf, return_atom_map=True)
        assert isomorphic is True
        for pdb_atom, sdf_atom in atom_map.items():
            assert mol.atoms[pdb_atom].to_dict() == mol_sdf.atoms[sdf_atom].to_dict()
        # check bonds match, however there order might not
        sdf_bonds = dict(((bond.atom1_index, bond.atom2_index), bond) for bond in mol_sdf.bonds)
        for bond in mol.bonds:
            key = (atom_map[bond.atom1_index], atom_map[bond.atom2_index])
            if key not in sdf_bonds:
                key = tuple(reversed(key))
            assert key in sdf_bonds
            # now compare the attributes
            assert bond.is_aromatic == sdf_bonds[key].is_aromatic
            assert bond.stereochemistry == sdf_bonds[key].stereochemistry

    def test_to_qcschema(self):
        """Test the ability to make and validate qcschema"""
        # the molecule has no coordinates so this should fail
        ethanol = Molecule.from_smiles('CCO')
        with pytest.raises(InvalidConformerError):
            qcschema = ethanol.to_qcschema()

        # now remake the molecule from the sdf
        ethanol = Molecule.from_file(get_data_file_path('molecules/ethanol.sdf'))
        # make sure that requests to missing conformers are caught
        with pytest.raises(InvalidConformerError):
            qcschema = ethanol.to_qcschema(conformer=1)
        # now make a valid qcschema and check its properties
        qcschema = ethanol.to_qcschema()
        # make sure the properties match
        charge = 0
        connectivity = [(0, 1, 1.0), (0, 3, 1.0), (0, 4, 1.0), (0, 5, 1.0), (1, 2, 1.0), (1, 6, 1.0), (1, 7, 1.0), (2, 8, 1.0)]
        symbols = ['C', 'C', 'O', 'H', 'H', 'H', 'H', 'H', 'H']
        assert charge == qcschema.molecular_charge
        assert connectivity == qcschema.connectivity
        assert symbols == qcschema.symbols.tolist()
        assert qcschema.geometry.all() == ethanol.conformers[0].in_units_of(unit.bohr).all()

    def test_from_qcschema_no_client(self):
        """Test the ability to make molecules from QCArchive record instances and dicts"""

        import json

        # As the method can take a record instance or a dict with JSON encoding test both
        # test incomplete dict
        example_dict = {'name': 'CH4'}
        with pytest.raises(KeyError):
            mol = Molecule.from_qcschema(example_dict)

        # test an object that is not a record
        wrong_object = 'CH4'
        with pytest.raises(AttributeError):
            mol = Molecule.from_qcschema(wrong_object)

        with open(get_data_file_path('molecules/qcportal_molecules.json')) as json_file:
            # test loading the dict representation from a json file
            json_mol = json.load(json_file)
            mol = Molecule.from_qcschema(json_mol)
            # now make a molecule from the canonical smiles and make sure they are isomorphic
            can_mol = Molecule.from_smiles(json_mol['attributes']['canonical_isomeric_smiles'])
            assert mol.is_isomorphic_with(can_mol) is True

    client_examples = [{'dataset': 'TorsionDriveDataset', 'name': 'Fragment Stability Benchmark', 'index':
                        'CC(=O)Nc1cc2c(cc1OC)nc[n:4][c:3]2[NH:2][c:1]3ccc(c(c3)Cl)F'},
                       {'dataset': 'TorsionDriveDataset', 'name': 'OpenFF Fragmenter Phenyl Benchmark', 'index':
                        'c1c[ch:1][c:2](cc1)[c:3](=[o:4])o'},
                       {'dataset': 'TorsionDriveDataset', 'name': 'OpenFF Full TorsionDrive Benchmark 1', 'index':
                        '0'},
                       {'dataset': 'TorsionDriveDataset', 'name': 'OpenFF Group1 Torsions', 'index':
                        'c1c[ch:1][c:2](cc1)[ch2:3][c:4]2ccccc2'},
                       {'dataset': 'OptimizationDataset', 'name': 'Kinase Inhibitors: WBO Distributions', 'index':
                        'cc1ccc(cc1nc2nccc(n2)c3cccnc3)nc(=o)c4ccc(cc4)cn5ccn(cc5)c-0'},
                       {'dataset': 'OptimizationDataset', 'name': 'SMIRNOFF Coverage Set 1', 'index':
                        'coc(o)oc-0'},
                       {'dataset': 'GridOptimizationDataset', 'name': 'OpenFF Trivalent Nitrogen Set 1', 'index':
                        'b1(c2c(ccs2)-c3ccsc3n1)c4c(c(c(c(c4f)f)f)f)f'},
                       {'dataset': 'GridOptimizationDataset', 'name': 'OpenFF Trivalent Nitrogen Set 1', 'index':
                       'C(#N)N'}
                       ]

    @pytest.mark.parametrize('input_data', client_examples)
    def test_from_qcschema_with_client(self, input_data):
        """For each of the examples try and make a offmol using the instance and dict and check they match"""

        import qcportal as ptl
        client = ptl.FractalClient()
        ds = client.get_collection(input_data['dataset'], input_data['name'])
        entry = ds.get_entry(input_data['index'])
        # now make the molecule from the record instance with and without the geometry
        mol_from_dict = Molecule.from_qcschema(entry.dict(encoding='json'))
        # make the molecule again with the geometries attached
        mol_from_instance = Molecule.from_qcschema(entry, client)
        if hasattr(entry, 'initial_molecules'):
            assert mol_from_instance.n_conformers == len(entry.initial_molecules)
        else:
            # opt records have one initial molecule
            assert mol_from_instance.n_conformers == 1

        # now make a molecule from the smiles and make sure they are isomorphic
        mol_from_smiles = Molecule.from_smiles(entry.attributes['canonical_explicit_hydrogen_smiles'], True)

        assert mol_from_dict.is_isomorphic_with(mol_from_smiles) is True

    def test_qcschema_round_trip(self):
        """Test making a molecule from qcschema then converting back"""

        # get a molecule qcschema
        import qcportal as ptl
        client = ptl.FractalClient()
        ds = client.get_collection('OptimizationDataset', 'SMIRNOFF Coverage Set 1')
        # grab an entry from the optimization data set
        entry = ds.get_entry('coc(o)oc-0')
        # now make the molecule from the record instance with the geometry
        mol = Molecule.from_qcschema(entry, client)
        # now grab the initial molecule record
        qca_mol = client.query_molecules(id=entry.initial_molecule)[0]
        # mow make sure the majority of the qcschema attributes are the same
        # note we can not compare the full dict due to qcelemental differences
        qcschema = mol.to_qcschema()
        assert qcschema.atom_labels.tolist() == qca_mol.atom_labels.tolist()
        assert qcschema.symbols.tolist() == qca_mol.symbols.tolist()
        # due to conversion useing different programs there is a slight difference here
        assert qcschema.geometry.flatten().tolist() == pytest.approx(qca_mol.geometry.flatten().tolist(), rel=1.0e-5)
        assert qcschema.connectivity == qca_mol.connectivity
        assert qcschema.atomic_numbers.tolist() == qca_mol.atomic_numbers.tolist()
        assert qcschema.fragment_charges == qca_mol.fragment_charges
        assert qcschema.fragment_multiplicities == qca_mol.fragment_multiplicities
        assert qcschema.fragments[0].tolist() == qca_mol.fragments[0].tolist()
        assert qcschema.mass_numbers.tolist() == qca_mol.mass_numbers.tolist()
        assert qcschema.name == qca_mol.name
        assert qcschema.masses.all() == qca_mol.masses.all()
        assert qcschema.molecular_charge == qca_mol.molecular_charge
        assert qcschema.molecular_multiplicity == qca_mol.molecular_multiplicity
        assert qcschema.real.all() == qca_mol.real.all()

    def test_from_mapped_smiles(self):
        """Test making the molecule from issue #412 using both toolkits to ensure the issue
        is fixed."""

        # there should be no undefined sterochmeistry error when making the molecule
        mol = Molecule.from_mapped_smiles('[H:14][c:1]1[c:3]([c:7]([c:11]([c:8]([c:4]1[H:17])[H:21])[C:13]([H:24])([H:25])[c:12]2[c:9]([c:5]([c:2]([c:6]([c:10]2[H:23])[H:19])[H:15])[H:18])[H:22])[H:20])[H:16]')
        assert mol.n_atoms == 25
        # make sure the atom map is not exposed
        with pytest.raises(KeyError):
            mapping = mol._properties['atom_map']

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_n_particles(self, molecule):
        """Test n_particles property"""
        n_particles = sum([1 for particle in molecule.particles])
        assert n_particles == molecule.n_particles

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_n_atoms(self, molecule):
        """Test n_atoms property"""
        n_atoms = sum([1 for atom in molecule.atoms])
        assert n_atoms == molecule.n_atoms

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_n_virtual_sites(self, molecule):
        """Test n_virtual_sites property"""
        n_virtual_sites = sum([1 for virtual_site in molecule.virtual_sites])
        assert n_virtual_sites == molecule.n_virtual_sites

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_n_bonds(self, molecule):
        """Test n_bonds property"""
        n_bonds = sum([1 for bond in molecule.bonds])
        assert n_bonds == molecule.n_bonds

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_angles(self, molecule):
        """Test angles property"""
        for angle in molecule.angles:
            assert angle[0].is_bonded_to(angle[1])
            assert angle[1].is_bonded_to(angle[2])

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_propers(self, molecule):
        """Test propers property"""
        for proper in molecule.propers:
            # The bonds should be in order 0-1-2-3 unless the
            # atoms form a three- or four-membered ring.
            is_chain = proper[0].is_bonded_to(proper[1])
            is_chain &= proper[1].is_bonded_to(proper[2])
            is_chain &= proper[2].is_bonded_to(proper[3])
            is_chain &= not proper[0].is_bonded_to(proper[2])
            is_chain &= not proper[0].is_bonded_to(proper[3])
            is_chain &= not proper[1].is_bonded_to(proper[3])

            assert (is_chain or
                    is_three_memebered_ring_torsion(proper) or
                    is_four_memebered_ring_torsion(proper))

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_impropers(self, molecule):
        """Test impropers property"""
        for improper in molecule.impropers:
            assert improper[0].is_bonded_to(improper[1])
            assert improper[1].is_bonded_to(improper[2])
            assert improper[1].is_bonded_to(improper[3])

            # The non-central atoms can be connected only if
            # the improper atoms form a three-membered ring.
            is_not_cyclic = not((improper[0].is_bonded_to(improper[2])) or
                                (improper[0].is_bonded_to(improper[3])) or
                                (improper[2].is_bonded_to(improper[3])))
            assert is_not_cyclic or is_three_memebered_ring_torsion(improper)

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_torsions(self, molecule):
        """Test torsions property"""
        # molecule.torsions should be exactly equal to the union of propers and impropers.
        assert set(molecule.torsions) == set(molecule.propers) | set(molecule.impropers)

        # The intersection of molecule.propers and molecule.impropers should be largely null.
        # The only exception is for molecules containing 3-membered rings (e.g., DrugBank_5514).
        common_torsions = molecule.propers & molecule.impropers
        if len(common_torsions) > 0:
            for torsion in common_torsions:
                assert is_three_memebered_ring_torsion(torsion)

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_total_charge(self, molecule):
        """Test total charge"""
        total_charge = sum([atom.formal_charge for atom in molecule.atoms])
        assert total_charge == molecule.total_charge

    # ----------------------------------------------------
    # Test magic methods.
    # ----------------------------------------------------

    def test_equality(self):
        """Test equality operator"""
        molecules = mini_drug_bank()
        nmolecules = len(molecules)
        # TODO: Performance improvements should let us un-restrict this test
        for i in range(nmolecules):
            for j in range(i, min(i+3, nmolecules)):
                assert (molecules[i] == molecules[j]) == (i == j)

    # ----------------------
    # Test Molecule methods.
    # ----------------------

    def test_add_conformers(self):
        """Test addition of conformers to a molecule"""
        import numpy as np
        from simtk import unit
        # Define a methane molecule
        molecule = Molecule()
        molecule.name = 'methane'
        C = molecule.add_atom(6, 0, False)
        H1 = molecule.add_atom(1, 0, False)
        H2 = molecule.add_atom(1, 0, False)
        H3 = molecule.add_atom(1, 0, False)
        H4 = molecule.add_atom(1, 0, False)
        molecule.add_bond(C, H1, 1, False)
        molecule.add_bond(C, H2, 1, False)
        molecule.add_bond(C, H3, 1, False)
        molecule.add_bond(C, H4, 1, False)

        assert molecule.n_conformers == 0
        # Add a conformer that should work
        conf1 = unit.Quantity(np.array([[ 1., 2.,3.] ,[4. ,5. ,6.],[7., 8., 9.],
                                        [10.,11.,12.],[13.,14.,15]]),
                              unit.angstrom)
        molecule.add_conformer(conf1)
        assert molecule.n_conformers == 1

        conf2 = unit.Quantity(np.array([[101., 102. ,103.], [104. ,105. ,106.], [107., 108., 109.],
                                        [110.,111.,112.],   [113.,114.,115]]),
                              unit.angstrom)
        molecule.add_conformer(conf2)
        assert molecule.n_conformers == 2

        # Add conformers with too few coordinates
        conf_missing_z = unit.Quantity(np.array([[101., 102. ,103.], [104. ,105. ,106.], [107., 108., 109.],
                                        [110.,111.,112.],   [113.,114.]]),
                                        unit.angstrom)
        with pytest.raises(Exception) as excinfo:
            molecule.add_conformer(conf_missing_z)

        conf_too_few_atoms = unit.Quantity(np.array([[101., 102. ,103.], [104. ,105. ,106.], [107., 108., 109.],
                                                     [110.,111.,112.]]),
                                                     unit.angstrom)
        with pytest.raises(Exception) as excinfo:
            molecule.add_conformer(conf_too_few_atoms)


        # Add a conformer with too many coordinates
        conf_too_many_atoms = unit.Quantity(np.array([[101., 102., 103.], [104., 105., 106.], [107., 108., 109.],
                                                      [110., 111., 112.], [113., 114., 115.], [116., 117., 118.]]),
                                            unit.angstrom)
        with pytest.raises(Exception) as excinfo:
            molecule.add_conformer(conf_too_many_atoms)

        # Add a conformer with no coordinates
        conf_no_coordinates = unit.Quantity(np.array([]),
                                            unit.angstrom)
        with pytest.raises(Exception) as excinfo:
            molecule.add_conformer(conf_no_coordinates)

        # Add a conforer with units of nanometers
        conf3 = unit.Quantity(np.array([[ 1., 2.,3.] ,[4. ,5. ,6.],[7., 8., 9.],
                                        [10.,11.,12.],[13.,14.,15]]),
                              unit.nanometer)
        molecule.add_conformer(conf3)
        assert molecule.n_conformers == 3
        assert molecule.conformers[2][0][0] == 10. * unit.angstrom

        # Add a conformer with units of nanometers
        conf_nonsense_units = unit.Quantity(np.array([[ 1., 2.,3.] ,[4. ,5. ,6.],[7., 8., 9.],
                                        [10.,11.,12.],[13.,14.,15]]),
                              unit.joule)
        with pytest.raises(Exception) as excinfo:
            molecule.add_conformer(conf_nonsense_units)

        # Add a conformer with no units
        conf_unitless = np.array([[ 1., 2.,3.] ,[4. ,5. ,6.],[7., 8., 9.],
                                  [10.,11.,12.],[13.,14.,15]])
        with pytest.raises(Exception) as excinfo:
            molecule.add_conformer(conf_unitless)

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_add_atoms_and_bonds(self, molecule):
        """Test the creation of a molecule from the addition of atoms and bonds"""
        molecule_copy = Molecule()
        for atom in molecule.atoms:
            molecule_copy.add_atom(atom.atomic_number, atom.formal_charge, atom.is_aromatic, stereochemistry=atom.stereochemistry)
        for bond in molecule.bonds:
            molecule_copy.add_bond(bond.atom1_index, bond.atom2_index, bond.bond_order, bond.is_aromatic,
                                   stereochemistry=bond.stereochemistry,
                                   fractional_bond_order=bond.fractional_bond_order)
        # Try to add the final bond twice, which should raise an Exception
        with pytest.raises(Exception) as excinfo:
            molecule_copy.add_bond(bond.atom1_index, bond.atom2_index, bond.bond_order, bond.is_aromatic,
                                   stereochemistry=bond.stereochemistry,
                                   fractional_bond_order=bond.fractional_bond_order)

        assert molecule == molecule_copy

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_add_virtual_site_units(self, molecule):
        """
        Tests the unit type checking of the VirtualSite base class
        """

        # TODO: Should these be using BondChargeVirtualSite, or should we just call the base class (which does the unit checks) directly?

        # Prepare values for unit checks
        distance_unitless = 0.4
        sigma_unitless = 0.1
        rmin_half_unitless = 0.2
        epsilon_unitless = 0.3
        charge_increments_unitless = [0.1, 0.2, 0.3, 0.4]
        distance = distance_unitless * unit.angstrom
        sigma = sigma_unitless * unit.angstrom
        rmin_half = rmin_half_unitless * unit.angstrom
        epsilon = epsilon_unitless * (unit.kilojoule / unit.mole)
        charge_increments = charge_increments_unitless * unit.elementary_charge

        # Do not modify the original molecule.
        molecule = copy.deepcopy(molecule)

        atom1 = molecule.atoms[0]
        atom2 = molecule.atoms[1]
        atom3 = molecule.atoms[2]
        atom4 = molecule.atoms[3]

        # Try to feed in unitless sigma
        with pytest.raises(Exception) as excinfo:
            molecule.add_bond_charge_virtual_site([atom1, atom2, atom3], distance, epsilon=epsilon, sigma=sigma_unitless)

        # Try to feed in unitless rmin_half
        with pytest.raises(Exception) as excinfo:
            molecule.add_bond_charge_virtual_site([atom1, atom2, atom3], distance, epsilon=epsilon, rmin_half=rmin_half_unitless)

        # Try to feed in unitless epsilon
        with pytest.raises(Exception) as excinfo:
            molecule.add_bond_charge_virtual_site([atom1, atom2, atom3], distance, epsilon=epsilon_unitless, sigma=sigma, rmin_half=rmin_half)

        # Try to feed in unitless charges
        with pytest.raises(Exception) as excinfo:
            molecule.add_bond_charge_virtual_site([atom1, atom2, atom3, atom4], distance, charge_incrtements=charge_increments_unitless)


        # We shouldn't be able to give both rmin_half and sigma VdW parameters.
        with pytest.raises(Exception) as excinfo:
            molecule.add_bond_charge_virtual_site([atom1, atom2, atom3], distance, epsilon=epsilon, sigma=sigma, rmin_half=rmin_half)

        # Try creating virtual site from sigma+epsilon
        vsite1_index = molecule.add_bond_charge_virtual_site([atom1, atom2, atom3], distance, epsilon=epsilon, sigma=sigma)
        # Try creating virutal site from rmin_half+epsilon
        vsite2_index = molecule.add_bond_charge_virtual_site([atom1, atom2, atom3], distance, epsilon=epsilon, rmin_half=rmin_half)

        # TODO: Test the @property getters for sigma, epsilon, and rmin_half

        # We should have to give as many charge increments as atoms (len(charge_increments)) = 4
        with pytest.raises(Exception) as excinfo:
            molecule.add_bond_charge_virtual_site([atom1, atom2, atom3], distance, charge_increments=charge_increments)

        vsite3_index = molecule.add_bond_charge_virtual_site([atom1, atom2, atom3, atom4], distance, charge_increments=charge_increments)

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_add_bond_charge_virtual_site(self, molecule):
        """Test the addition of a BondChargeVirtualSite to a molecule.
           Also tests many of the inputs of the parent VirtualSite class
        """
        # Do not modify the original molecule.
        molecule = copy.deepcopy(molecule)

        atom1 = molecule.atoms[0]
        atom2 = molecule.atoms[1]
        atom3 = molecule.atoms[2]
        atom4 = molecule.atoms[3]

        # Prepare values for unit checks
        distance_unitless = 0.4
        distance = distance_unitless * unit.angstrom


        # Try to feed in a unitless distance
        with pytest.raises(AssertionError) as excinfo:
            vsite1_index = molecule.add_bond_charge_virtual_site([atom1, atom2, atom3], distance_unitless)


        vsite1_index = molecule.add_bond_charge_virtual_site([atom1, atom2, atom3], distance)
        vsite1 = molecule.virtual_sites[vsite1_index]
        assert atom1 in vsite1.atoms
        assert atom2 in vsite1.atoms
        assert atom3 in vsite1.atoms
        assert vsite1 in atom1.virtual_sites
        assert vsite1 in atom2.virtual_sites
        assert vsite1 in atom3.virtual_sites
        assert vsite1.distance == distance

        # Make an "everything bagel" virtual site
        vsite2_index = molecule.add_bond_charge_virtual_site([atom1, atom2, atom3],
                                                             distance,
                                                             sigma=0.1*unit.angstrom,
                                                             epsilon=1.0*unit.kilojoule_per_mole,
                                                             charge_increments=unit.Quantity(np.array([0.1, 0.2, 0.3]),
                                                                                             unit.elementary_charge)
                                                             )
        vsite2 = molecule.virtual_sites[vsite2_index]

        # test serialization
        molecule_dict = molecule.to_dict()
        molecule2 = Molecule.from_dict(molecule_dict)

        assert hash(molecule) == hash(molecule2)

    # TODO: Make a test for to_dict and from_dict for VirtualSites (even though they're currently just unloaded using
    #      (for example) Molecule._add_bond_virtual_site functions
    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_add_monovalent_lone_pair_virtual_site(self, molecule):
        """Test addition of a MonovalentLonePairVirtualSite to the Molecule"""
        # Do not modify the original molecule.
        molecule = copy.deepcopy(molecule)

        atom1 = molecule.atoms[0]
        atom2 = molecule.atoms[1]
        atom3 = molecule.atoms[2]
        atom4 = molecule.atoms[3]

        # Prepare values for unit checks
        distance_unitless = 0.3
        out_of_plane_angle_unitless = 30
        in_plane_angle_unitless = 0.2
        distance = distance_unitless * unit.angstrom
        out_of_plane_angle = out_of_plane_angle_unitless * unit.degree
        in_plane_angle = in_plane_angle_unitless * unit.radian

        # Try passing in a unitless distance
        with pytest.raises(AssertionError) as excinfo:
            vsite1_index = molecule.add_monovalent_lone_pair_virtual_site([atom1, atom2], distance_unitless, out_of_plane_angle, in_plane_angle)

        # Try passing in a unitless out_of_plane_angle
        with pytest.raises(AssertionError) as excinfo:
            vsite1_index = molecule.add_monovalent_lone_pair_virtual_site([atom1, atom2], distance, out_of_plane_angle_unitless, in_plane_angle)

        # Try passing in a unitless in_plane_angle
        with pytest.raises(AssertionError) as excinfo:
            vsite1_index = molecule.add_monovalent_lone_pair_virtual_site([atom1, atom2], distance, out_of_plane_angle, in_plane_angle_unitless)

        # Try giving two atoms
        with pytest.raises(AssertionError) as excinfo:
            vsite1_index = molecule.add_monovalent_lone_pair_virtual_site([atom1, atom2], distance, out_of_plane_angle, in_plane_angle)

        # Successfully make a virtual site
        vsite1_index = molecule.add_monovalent_lone_pair_virtual_site([atom1, atom2, atom3], distance, out_of_plane_angle, in_plane_angle)
        # TODO: Check if we get the same values back out from the @properties
        molecule_dict = molecule.to_dict()
        molecule2 = Molecule.from_dict(molecule_dict)
        assert molecule.to_dict() == molecule2.to_dict()

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_add_divalent_lone_pair_virtual_site(self, molecule):
        """Test addition of a DivalentLonePairVirtualSite to the Molecule"""
        # Do not modify the original molecule.
        molecule = copy.deepcopy(molecule)

        atom1 = molecule.atoms[0]
        atom2 = molecule.atoms[1]
        atom3 = molecule.atoms[2]
        atom4 = molecule.atoms[3]
        distance = 0.3 * unit.angstrom
        out_of_plane_angle = 30 * unit.degree
        in_plane_angle = 0.2 * unit.radian
        vsite1_index = molecule.add_divalent_lone_pair_virtual_site([atom1, atom2, atom3], distance, out_of_plane_angle, in_plane_angle)
        with pytest.raises(AssertionError) as excinfo:
            vsite1_index = molecule.add_divalent_lone_pair_virtual_site([atom1, atom2], distance, out_of_plane_angle, in_plane_angle)
        molecule_dict = molecule.to_dict()
        molecule2 = Molecule.from_dict(molecule_dict)
        assert molecule_dict == molecule2.to_dict()

    @pytest.mark.parametrize('molecule', mini_drug_bank())
    def test_add_trivalent_lone_pair_virtual_site(self, molecule):
        """Test addition of a TrivalentLonePairVirtualSite to the Molecule"""
        # Do not modify the original molecule.
        molecule = copy.deepcopy(molecule)

        atom1 = molecule.atoms[0]
        atom2 = molecule.atoms[1]
        atom3 = molecule.atoms[2]
        atom4 = molecule.atoms[3]
        distance = 0.3 * unit.angstrom
        out_of_plane_angle = 30 * unit.degree
        in_plane_angle = 0.2 * unit.radian
        vsite1_index = molecule.add_trivalent_lone_pair_virtual_site([atom1, atom2, atom3, atom4], distance, out_of_plane_angle, in_plane_angle)
        # Test for assertion when giving too few atoms
        with pytest.raises(AssertionError) as excinfo:
            vsite1_index = molecule.add_trivalent_lone_pair_virtual_site([atom1, atom2, atom3], distance, out_of_plane_angle, in_plane_angle)
        molecule_dict = molecule.to_dict()
        molecule2 = Molecule.from_dict(molecule_dict)
        assert molecule.to_dict() == molecule2.to_dict()

    @requires_openeye
    def test_chemical_environment_matches_OE(self):
        """Test chemical environment matches"""
        # TODO: Move this to test_toolkits, test all available toolkits
        # Create chiral molecule
        from simtk.openmm.app import element
        toolkit_wrapper = OpenEyeToolkitWrapper()
        molecule = Molecule()
        atom_C = molecule.add_atom(element.carbon.atomic_number, 0, False, stereochemistry='R', name='C')
        atom_H = molecule.add_atom(element.hydrogen.atomic_number, 0, False, name='H')
        atom_Cl = molecule.add_atom(element.chlorine.atomic_number, 0, False, name='Cl')
        atom_Br = molecule.add_atom(element.bromine.atomic_number, 0, False, name='Br')
        atom_F = molecule.add_atom(element.fluorine.atomic_number, 0, False, name='F')
        molecule.add_bond(atom_C, atom_H, 1, False)
        molecule.add_bond(atom_C, atom_Cl, 1, False)
        molecule.add_bond(atom_C, atom_Br, 1, False)
        molecule.add_bond(atom_C, atom_F, 1, False)
        # Test known cases
        matches = molecule.chemical_environment_matches('[#6:1]', toolkit_registry=toolkit_wrapper)
        assert len(matches) == 1 # there should be a unique match, so one atom tuple is returned
        assert len(matches[0]) == 1 # it should have one tagged atom
        assert set(matches[0]) == set([atom_C])
        matches = molecule.chemical_environment_matches('[#6:1]~[#1:2]', toolkit_registry=toolkit_wrapper)
        assert len(matches) == 1 # there should be a unique match, so one atom tuple is returned
        assert len(matches[0]) == 2 # it should have two tagged atoms
        assert set(matches[0]) == set([atom_C, atom_H])
        matches = molecule.chemical_environment_matches('[Cl:1]-[C:2]-[H:3]', toolkit_registry=toolkit_wrapper)
        assert len(matches) == 1 # there should be a unique match, so one atom tuple is returned
        assert len(matches[0]) == 3 # it should have three tagged atoms
        assert set(matches[0]) == set([atom_Cl, atom_C, atom_H])
        matches = molecule.chemical_environment_matches('[#6:1]~[*:2]', toolkit_registry=toolkit_wrapper)
        assert len(matches) == 4 # there should be four matches
        for match in matches:
            assert len(match) == 2 # each match should have two tagged atoms

    # TODO: Test forgive undef amide enol stereo
    # TODO: test forgive undef phospho linker stereo
    # TODO: test forgive undef C=NH stereo
    # TODO: test forgive undef phospho stereo
    # Potentially better OE stereo check: OEFlipper — Toolkits - - Python
    # https: // docs.eyesopen.com / toolkits / python / omegatk / OEConfGenFunctions / OEFlipper.html

    @requires_rdkit
    def test_chemical_environment_matches_RDKit(self):
        """Test chemical environment matches"""
        # Create chiral molecule
        from simtk.openmm.app import element
        toolkit_wrapper = RDKitToolkitWrapper()
        molecule = Molecule()
        atom_C = molecule.add_atom(element.carbon.atomic_number, 0, False, stereochemistry='R', name='C')
        atom_H = molecule.add_atom(element.hydrogen.atomic_number, 0, False, name='H')
        atom_Cl = molecule.add_atom(element.chlorine.atomic_number, 0, False, name='Cl')
        atom_Br = molecule.add_atom(element.bromine.atomic_number, 0, False, name='Br')
        atom_F = molecule.add_atom(element.fluorine.atomic_number, 0, False, name='F')
        molecule.add_bond(atom_C, atom_H, 1, False)
        molecule.add_bond(atom_C, atom_Cl, 1, False)
        molecule.add_bond(atom_C, atom_Br, 1, False)
        molecule.add_bond(atom_C, atom_F, 1, False)
        # Test known cases
        matches = molecule.chemical_environment_matches('[#6:1]', toolkit_registry=toolkit_wrapper)
        assert len(matches) == 1 # there should be a unique match, so one atom tuple is returned
        assert len(matches[0]) == 1 # it should have one tagged atom
        assert set(matches[0]) == set([atom_C])
        matches = molecule.chemical_environment_matches('[#6:1]~[#1:2]', toolkit_registry=toolkit_wrapper)
        assert len(matches) == 1 # there should be a unique match, so one atom tuple is returned
        assert len(matches[0]) == 2 # it should have two tagged atoms
        assert set(matches[0]) == set([atom_C, atom_H])
        matches = molecule.chemical_environment_matches('[Cl:1]-[C:2]-[H:3]', toolkit_registry=toolkit_wrapper)
        assert len(matches) == 1 # there should be a unique match, so one atom tuple is returned
        assert len(matches[0]) == 3 # it should have three tagged atoms
        assert set(matches[0]) == set([atom_Cl, atom_C, atom_H])
        matches = molecule.chemical_environment_matches('[#6:1]~[*:2]', toolkit_registry=toolkit_wrapper)
        assert len(matches) == 4 # there should be four matches
        for match in matches:
            assert len(match) == 2 # each match should have two tagged atoms

    @pytest.mark.slow
    def test_compute_partial_charges(self):
        """Test computation/retrieval of partial charges"""
        # TODO: Test only one molecule for speed?
        # TODO: Do we need to deepcopy each molecule, or is setUp called separately for each test method?
        from simtk import unit
        import numpy as np

        # Do not modify original molecules.
        molecules = copy.deepcopy(mini_drug_bank())

        # Test a single toolkit at a time
        # Removed  ['amber', 'amberff94'] from OE list, as those won't find the residue types they're expecting
        toolkit_to_charge_method = {OpenEyeToolkitWrapper:['mmff', 'mmff94', 'am1bcc', 'am1bccnosymspt', 'am1bccelf10'],
                                   AmberToolsToolkitWrapper:['bcc', 'gas', 'mul']}

        manual_skips = []

        manual_skips.append('ZINC1564378') # Warning: OEMMFF94Charges: assigning OEMMFFAtomTypes failed on mol .
        manual_skips.append('ZINC00265517') # Warning: OEMMFF94Charges: assigning OEMMFFAtomTypes failed on mol .

        for toolkit in list(toolkit_to_charge_method.keys()):
            toolkit_registry = ToolkitRegistry(toolkit_precedence=[toolkit])
            for charge_model in toolkit_to_charge_method[toolkit]:
                c = 0
                for molecule in molecules[:1]: # Just test first molecule to save time
                    c += 1
                    if molecule.name in manual_skips:  # Manual skips, hopefully rare
                        continue
                    molecule.compute_partial_charges(charge_model=charge_model, toolkit_registry=toolkit_registry)
                    charges1 = molecule._partial_charges
                    # Make sure everything isn't 0s
                    assert (abs(charges1 / unit.elementary_charge) > 0.01).any()
                    # Check total charge
                    charges_sum_unitless = charges1.sum() / unit.elementary_charge
                    #if abs(charges_sum_unitless - float(molecule.total_charge)) > 0.0001:
                    #    print('c {}  molecule {}    charge_sum {}     molecule.total_charge {}'.format(c, molecule.name,
                    #                                                                                   charges_sum_unitless,
                    #                                                                                   molecule.total_charge))
                    # assert_almost_equal(charges_sum_unitless, molecule.total_charge, decimal=4)

                    # Call should be faster second time due to caching
                    # TODO: Implement caching
                    molecule.compute_partial_charges(charge_model=charge_model, toolkit_registry=toolkit_registry)
                    charges2 = molecule._partial_charges
                    assert (np.allclose(charges1, charges2, atol=0.002))

    @requires_openeye
    def test_assign_fractional_bond_orders(self):
        """Test assignment of fractional bond orders
        """
        # TODO: Test only one molecule for speed?
        # TODO: Do we need to deepcopy each molecule, or is setUp called separately for each test method?

        # Do not modify the original molecules.
        molecules = copy.deepcopy(mini_drug_bank())

        toolkits_to_bondorder_method = {(OpenEyeToolkitWrapper,):['am1-wiberg','pm3-wiberg']}
        # Don't test AmberTools here since it takes too long
                                       #(AmberToolsToolkitWrapper, RDKitToolkitWrapper):['am1-wiberg']}
        for toolkits in list(toolkits_to_bondorder_method.keys()):
            toolkit_registry = ToolkitRegistry(toolkit_precedence=toolkits)
            for bond_order_model in toolkits_to_bondorder_method[toolkits]:
                for molecule in molecules[:5]: # Just test first five molecules for speed
                    molecule.generate_conformers(toolkit_registry=toolkit_registry)
                    molecule.assign_fractional_bond_orders(bond_order_model=bond_order_model,
                                                           toolkit_registry=toolkit_registry,
                                                           use_conformers=molecule.conformers)
                    fbo1 = [bond.fractional_bond_order for bond in molecule.bonds]
                    # TODO: Now that the assign_fractional_bond_orders function takes more kwargs,
                    #       how can we meaningfully cache its results?
                    # # Call should be faster the second time due to caching
                    # molecule.assign_fractional_bond_orders(bond_order_model=bond_order_model,
                    #                                        toolkit_registry=toolkit_registry)
                    # fbo2 = [bond.fractional_bond_order for bond in molecule.bonds]
                    # np.testing.assert_allclose(fbo1, fbo2, atol=1.e-4)
