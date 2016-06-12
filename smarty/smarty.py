#!/usr/bin/env python

#=============================================================================================
# MODULE DOCSTRING
#=============================================================================================

"""
smarty.py

Example illustrating a scheme to create and destroy atom types automatically using SMARTS.

AUTHORS

John Chodera <jchodera@berkeley.edu>, University of California, Berkeley

The AtomTyper class is based on 'patty' by Pat Walters, Vertex Pharmaceuticals.

"""
#=============================================================================================
# GLOBAL IMPORTS
#=============================================================================================

import sys
import string

from optparse import OptionParser # For parsing of command line arguments

import os
import math
import copy
import re
import numpy
import random

import openeye.oechem
import openeye.oeomega
import openeye.oequacpac

from openeye.oechem import *
from openeye.oeomega import *
from openeye.oequacpac import *

import networkx

import time

#=============================================================================================
# ATOM TYPER
#=============================================================================================

class AtomTyper(object):
    """
    Atom typer based on SMARTS-defined atom types.

    Based on 'Patty' implementation by Pat Walters.

    """

    class TypingException(Exception):
        """
        Atom typing exception.

        """
        def __init__(self, molecule, atom):
            self.molecule = molecule
            self.atom = atom

        def __str__(self):
            msg = "Atom not assigned: molecule %s : atom index %6d name %8s element %8s" % (self.molecule.GetTitle(), self.atom.GetIdx(), self.atom.GetName(), OEGetAtomicSymbol(self.atom.GetAtomicNum()))
            msg += '\n'
            for atom in self.molecule.GetAtoms():
                msg += 'atom %8d : name %8s element %8s' % (atom.GetIdx(), atom.GetName(), OEGetAtomicSymbol(self.atom.GetAtomicNum()))
                if atom == self.atom:
                    msg += '  ***'
                msg += '\n'

            return msg

    def __init__(self, typelist, tagname, replacements=None):
        """"
        Create an atom typer instance.

        ARGUMENTS

        typelist : str
            If specified, will read types from list with each element [smarts, typename]
        tagname : str
            Tag name
        replacements : list of [smarts, shortname]
            Substitution/replacement bindings.

        """

        self.pattyTag = OEGetTag(tagname)

        # Create bindings list.
        bindings = list()
        if replacements is not None:
            for [smarts,shortname] in replacements:
                bindings.append( (shortname, smarts) )

        # Create table of search objects.
        self.smartsList = []
        for [smarts, typename] in typelist:
            # Perform binding replacements
            smarts = OESmartsLexReplace(smarts, bindings)
            # Create SMARTS search
            pat = OESubSearch()
            pat.Init(smarts)
            pat.SetMaxMatches(0)
            self.smartsList.append([pat,typename,smarts])

        return

    def dump(self):
        for pat,type,smarts in self.smartsList:
            print pat,type,smarts
        return

    def assignTypes(self,mol):
        # Assign null types.
        for atom in mol.GetAtoms():
            atom.SetStringData(self.pattyTag, "")

        # Assign atom types using rules.
        OEAssignAromaticFlags(mol)
        for pat,type,smarts in self.smartsList:
            for matchbase in pat.Match(mol):
                for matchpair in matchbase.GetAtoms():
                    matchpair.target.SetStringData(self.pattyTag,type)

        # Check if any atoms remain unassigned.
        for atom in mol.GetAtoms():
            if atom.GetStringData(self.pattyTag)=="":
                raise AtomTyper.TypingException(mol, atom)
        return

    def debugTypes(self,mol):
        for atom in mol.GetAtoms():
            print "%6d %8s %8s" % (atom.GetIdx(),OEGetAtomicSymbol(atom.GetAtomicNum()),atom.GetStringData(self.pattyTag))
        return

    def getTypeList(self,mol):
        typeList = []
        for atom in mol.GetAtoms():
            typeList.append(atom.GetStringData(self.pattyTag))
        return typeList

    @classmethod
    def read_typelist(cls, filename):
        """
        Read an atomtype or decorator list from a file.

        Parameters
        ----------
        filename : str
            The name of the file to be read

        Returns
        -------
        typelist : list of tuples
            Typelist[i] is element i of the typelist in format [smarts, typename]

        """
        if filename is None:
            return None

        if not os.path.exists(filename):
            raise Exception("File '%s' not found." % filename)

        typelist = list()
        ifs = open(filename)
        lines = ifs.readlines()
        for line in lines:
            # Strip trailing comments
            index = line.find('%')
            if index != -1:
                line = line[0:index]
            # Split into tokens.
            tokens = string.split(line)
            if len(tokens) == 2:
                [smarts,typename] = tokens
                typelist.append([smarts,typename])
        ifs.close()

        return typelist

#=============================================================================================
# ATOMTYPE SAMPLER
#=============================================================================================

class AtomTypeSampler(object):
    """
    Atom type sampler.

    """
    def __init__(self, molecules, basetypes_filename, decorators_filename, replacements_filename=None, reference_typed_molecules=None, temperature=0.1, verbose=False):
        """
        Initialize an atom type sampler.

        ARGUMENTS

        molecules : list of molecules for typing
            List of molecules for typing
        basetypes_filename : str
            File defining base atom types (which cannot be destroyed)
        decorators_filename : str
            File containing decorators that can be added to existing types to generate subtypes
        replacements_filename : str, optional, default=None
            If specified, SMARTS replacement definitions will be read from this file
        reference_typed_molecules : list of OEMol, optional, default=None
            List of molecules with reference types for use in Monte Carlo acceptance.
            If specified, the likelihood function will utilize the maximal number of matched atom types with these molecules.
            If not specified, no likelihood function will be employed.
        temperature : float, optional, default=0.1
            Temperature for Monte Carlo acceptance/rejection
        verbose : bool, optional, default=False
            If True, verbose output will be printed.

        Notes
        -----
        This is just a proof of concept.  No scoring of molecular properties is performed.

        """

        self.verbose = verbose

        # Define internal typing tag.
        self.typetag = 'atomtype'

        # Read atomtypes and decorators.
        self.atomtypes = AtomTyper.read_typelist(basetypes_filename)
        self.decorators = AtomTyper.read_typelist(decorators_filename)
        self.replacements = AtomTyper.read_typelist(replacements_filename)

        # Store a deep copy of the molecules since they will be annotated
        self.molecules = copy.deepcopy(molecules)

        # Type all molecules with current typelist to ensure that basetypes are sufficient.
        self.type_molecules(self.atomtypes, self.molecules)

        # Compute atomtype statistics on molecules.
        [atom_typecounts, molecule_typecounts] = self.compute_type_statistics(self.atomtypes, self.molecules)
        self.show_type_statistics(self.atomtypes, atom_typecounts, molecule_typecounts)

        # Compute total atoms
        self.total_atoms = 0.0
        for molecule in self.molecules:
            for atom in molecule.GetAtoms():
                self.total_atoms += 1.0

        # Store reference molecules
        self.reference_typed_molecules = None
        self.reference_atomtypes = set()
        self.current_atom_matches = None
        self.temperature = temperature
        if reference_typed_molecules is not None:
            self.reference_typed_molecules = copy.deepcopy(reference_typed_molecules)
            # Extract list of reference atom types
            for molecule in reference_typed_molecules:
                for atom in molecule.GetAtoms():
                    self.reference_atomtypes.add(atom.GetType())
            self.reference_atomtypes = list(self.reference_atomtypes)
            # Compute current atom matches
            [self.atom_type_matches, self.total_atom_type_matches] = self.best_match_reference_types(self.atomtypes, self.molecules)

        return

    def best_match_reference_types(self, atomtypes, molecules):
        """
        Determine best match for each parameter with reference atom types

        Parameters
        ----------
        atomtypes :
            Current atom types
        molecules : list of OEMol
            Typed molecules, where types are stored in self.atomtypetag string data.

        Returns
        -------
        atom_type_matches : list of tuples (current_atomtype, reference_atomtype, counts)
            Best correspondence between current and reference atomtypes, along with number of atoms equivalently typed in reference molecule set.
        total_atom_type_matches : int
            The total number of correspondingly typed atoms in the reference molecule set.

        * Currently, types for reference typed molecules are accessed via atom.GetType(), while types for current typed molecules are accessed via atom.GetStringData(self.typetag).
          This should be homogenized.

        Contributor:
        * Josh Fass <josh.fass@choderalab.org> contributed this algorithm.

        """
        if self.reference_typed_molecules is None:
            if self.verbose: print('No reference molecules specified, so skipping likelihood calculation.')
            return None

        # Create bipartite graph (U,V,E) matching current atom types U with reference atom types V via edges E with weights equal to number of atoms typed in common.
        if self.verbose: print('Creating graph matching current atom types with reference atom types...')
        initial_time = time.time()
        import networkx as nx
        graph = nx.Graph()
        # Add current atom types
        current_atomtypes = [ typename for (smarts, typename) in atomtypes ]
        for atomtype in current_atomtypes:
            graph.add_node(atomtype, bipartite=0)
        # Add reference atom types
        reference_atomtypes = [ typename for typename in self.reference_atomtypes ]
        for atomtype in reference_atomtypes:
            graph.add_node(atomtype, bipartite=1)
        # Add edges.
        atoms_in_common = dict()
        for current_atomtype in current_atomtypes:
            for reference_atomtype in reference_atomtypes:
                atoms_in_common[(current_atomtype,reference_atomtype)] = 0
        for (current_typed_molecule, reference_typed_molecule) in zip(molecules, self.reference_typed_molecules):
            for (current_typed_atom, reference_typed_atom) in zip(current_typed_molecule.GetAtoms(), reference_typed_molecule.GetAtoms()):
                current_atomtype = current_typed_atom.GetStringData(self.typetag)
                reference_atomtype = reference_typed_atom.GetType()
                atoms_in_common[(current_atomtype,reference_atomtype)] += 1
        for current_atomtype in current_atomtypes:
            for reference_atomtype in reference_atomtypes:
                weight = atoms_in_common[(current_atomtype,reference_atomtype)]
                graph.add_edge(current_atomtype, reference_atomtype, weight=weight)
        elapsed_time = time.time() - initial_time
        if self.verbose: print('Graph creation took %.3f s' % elapsed_time)

        # Compute maximum match
        if self.verbose: print('Computing maximum weight match...')
        initial_time = time.time()
        mate = nx.algorithms.max_weight_matching(graph, maxcardinality=False)
        elapsed_time = time.time() - initial_time
        if self.verbose: print('Maximum weight match took %.3f s' % elapsed_time)

        # Compute match dictionary and total number of matches.
        atom_type_matches = list()
        total_atom_type_matches = 0
        for current_atomtype in current_atomtypes:
            if current_atomtype in mate:
                reference_atomtype = mate[current_atomtype]
                counts = graph[current_atomtype][reference_atomtype]['weight']
                total_atom_type_matches += counts
                atom_type_matches.append( (current_atomtype, reference_atomtype, counts) )
            else:
                atom_type_matches.append( (current_atomtype, None, None) )

        # Report on matches
        if self.verbose:
            print("PROPOSED:")
            self.show_type_matches(atom_type_matches)

        return (atom_type_matches, total_atom_type_matches)

    def show_type_matches(self, atom_type_matches):
        """
        Show pairing of current to reference atom types.

        atom_type_matches : list of (current_atomtype, reference_atomtype, counts)
            List of atom type matches.

        """
        print('Atom type matches:')
        total_atom_type_matches = 0
        for (current_atomtype, reference_atomtype, counts) in atom_type_matches:
            if reference_atomtype is not None:
                print('%-64s matches %8s : %8d atoms matched' % (current_atomtype, reference_atomtype, counts))
                total_atom_type_matches += counts
            else:
                print('%-64s         no match' % (current_atomtype))

        fraction_matched_atoms = float(total_atom_type_matches) / float(self.total_atoms)
        print('%d / %d total atoms match (%.3f %%)' % (total_atom_type_matches, self.total_atoms, fraction_matched_atoms * 100))


    def sample_atomtypes(self):
        """
        Perform one step of atom type sampling.

        """
        # Copy current atomtypes for proposal.
        proposed_atomtypes = copy.deepcopy(self.atomtypes)
        proposed_molecules = copy.deepcopy(self.molecules)
        natomtypes = len(proposed_atomtypes)
        ndecorators = len(self.decorators)

        valid_proposal = True

        if random.random() < 0.5:
            # Pick an atom type to destroy.
            atomtype_index = random.randint(0, natomtypes-1)
            (atomtype, typename) = proposed_atomtypes[atomtype_index]
            if self.verbose: print("Attempting to destroy atom type %s : %s..." % (atomtype, typename))
            # Delete the atomtype.
            proposed_atomtypes.remove([atomtype, typename])
            # Try to type all molecules.
            try:
                self.type_molecules(proposed_atomtypes, proposed_molecules)
            except AtomTyper.TypingException as e:
                #print e
                # Reject since typing failed.
                if self.verbose: print("Typing failed; rejecting.")
                valid_proposal = False
        else:
            # Pick an atomtype to subtype.
            atomtype_index = random.randint(0, natomtypes-1)
            # Pick a decorator to add.
            decorator_index = random.randint(0, ndecorators-1)
            # Create new atomtype to insert by appending decorator with 'and' operator.
            (atomtype, atomtype_typename) = self.atomtypes[atomtype_index]
            (decorator, decorator_typename) = self.decorators[decorator_index]
            result = re.match('\[(.+)\]', atomtype)
            proposed_atomtype = '[' + result.groups(1)[0] + '&' + decorator + ']'
            proposed_typename = atomtype_typename + ' ' + decorator_typename
            print("Attempting to create new subtype: '%s' (%s) + '%s' (%s) -> '%s' (%s)" % (atomtype, atomtype_typename, decorator, decorator_typename, proposed_atomtype, proposed_typename))
            # Check if proposed atomtype is already in set.
            existing_atomtypes = set()
            for (a, b) in self.atomtypes:
                existing_atomtypes.add(a)
            if proposed_atomtype in existing_atomtypes:
                if self.verbose: print("Atom type already exists; rejecting to avoid duplication.")
                valid_proposal = False

            # Check for valid proposal before proceeding.
            if not valid_proposal:
                return False

            # Insert atomtype immediately after.
            proposed_atomtypes.insert(atomtype_index+1, [proposed_atomtype, proposed_typename])
            # Try to type all molecules.
            try:
                # Type molecules.
                self.type_molecules(proposed_atomtypes, proposed_molecules)
                # Compute updated statistics.
                [proposed_atom_typecounts, proposed_molecule_typecounts] = self.compute_type_statistics(proposed_atomtypes, proposed_molecules)
                # Reject if new type is unused.
                if (proposed_atom_typecounts[proposed_typename] == 0):
                    # Reject because new type is unused in dataset.
                    if self.verbose: print("Atom type '%s' (%s) unused in dataset; rejecting." % (proposed_atomtype, proposed_typename))
                    valid_proposal = False
                # Reject if parent type is now unused.
                if (proposed_atom_typecounts[atomtype_typename] == 0):
                    # Reject because new type is unused in dataset.
                    if self.verbose: print("Parent type '%s' (%s) now unused in dataset; rejecting." % (atomtype, atomtype_typename))
                    valid_proposal = False
            except AtomTyper.TypingException as e:
                print("Exception: %s" % str(e))
                # Reject since typing failed.
                if self.verbose: print("Typing failed for one or more molecules using proposed atomtypes; rejecting.")
                valid_proposal = False

        # Check for valid proposal
        if not valid_proposal:
            return False

        if self.verbose: print('Proposal is valid...')

        if self.temperature == 0.0:
            effective_temperature = 1
        else:
            effective_temperature = (self.total_atoms * self.temperature)

        # Compute likelihood for accept/reject
        (proposed_atom_type_matches, proposed_total_atom_type_matches) = self.best_match_reference_types(proposed_atomtypes, proposed_molecules)
        log_P_accept = (proposed_total_atom_type_matches - self.total_atom_type_matches) / effective_temperature
        if (log_P_accept > 0.0) or (numpy.random.uniform() < numpy.exp(log_P_accept)):
            # Accept.
            self.atomtypes = proposed_atomtypes
            self.molecules = proposed_molecules
            self.atom_type_matches = proposed_atom_type_matches
            self.total_atom_type_matches = proposed_total_atom_type_matches
            return True
        else:
            return False

    def type_molecules(self, typelist, molecules):
        """
        Type all molecules with the specified typelist.

        """
        # Create an atom typer.
        atomtyper = AtomTyper(typelist, self.typetag, replacements=self.replacements)

        # Type molecules.
        for molecule in molecules:
            atomtyper.assignTypes(molecule)

        return

    def compute_type_statistics(self, typelist, molecules):
        """
        Compute statistics for numnber of molecules assigned each type.

        ARGUMENTS

        typelist
        molecules

        RETURNS

        atom_typecounts (dict) - counts of number of atoms containing each atomtype
        molecule_typecounds (dict) - counts of number of molecules containing each atom type

        """
        # Zero type counts by atom and molecule.
        atom_typecounts = dict()
        molecule_typecounts = dict()
        for [smarts, typename] in typelist:
            atom_typecounts[typename] = 0
            molecule_typecounts[typename] = 0

        # Count number of atoms with each type.
        for molecule in molecules:
            types_in_this_molecule = set()
            for atom in molecule.GetAtoms():
                atomtype = atom.GetStringData(self.typetag)
                types_in_this_molecule.add(atomtype)
                atom_typecounts[atomtype] += 1
            for atomtype in types_in_this_molecule:
                molecule_typecounts[atomtype] += 1

        return (atom_typecounts, molecule_typecounts)

    def show_type_statistics(self, typelist, atom_typecounts, molecule_typecounts, atomtype_matches=None):
        """
        Print atom type statistics.

        """
        index = 1
        natoms = 0

        if atomtype_matches is not None:
            reference_type_info = dict()
            for (typename, reference_atomtype, count) in atomtype_matches:
                reference_type_info[typename] = (reference_atomtype, count)

        # Print header
        if atomtype_matches is not None:
            print "%5s   %10s %10s   %48s %48s %16s %16s" % ('INDEX', 'ATOMS', 'MOLECULES', 'TYPE NAME', 'SMARTS', 'REFERENCE TYPE', 'REFERENCE COUNT')
        else:
            print "%5s   %10s %10s   %48s %48s" % ('INDEX', 'ATOMS', 'MOLECULES', 'TYPE NAME', 'SMARTS')

        # Print counts
        for [smarts, typename] in typelist:
            if atomtype_matches is not None:
                (reference_atomtype, reference_count) = reference_type_info[typename]
                if reference_atomtype is not None:
                    print "%5d : %10d %10d | %48s %48s %16s %16d" % (index, atom_typecounts[typename], molecule_typecounts[typename], typename, smarts, reference_atomtype, reference_count)
                else:
                    print "%5d : %10d %10d | %48s %48s %16s %16s" % (index, atom_typecounts[typename], molecule_typecounts[typename], typename, smarts, '', '')
            else:
                print "%5d : %10d %10d | %48s %48s" % (index, atom_typecounts[typename], molecule_typecounts[typename], typename, smarts)

            natoms += atom_typecounts[typename]
            index += 1

        nmolecules = len(self.molecules)

        if atomtype_matches is not None:
            print "%5s : %10d %10d |  %48s %48s %10d / %10d match (%.3f %%)" % ('TOTAL', natoms, nmolecules, '', '', self.total_atom_type_matches, self.total_atoms, (float(self.total_atom_type_matches) / float(self.total_atoms)) * 100)
        else:
            print "%5s : %10d %10d" % ('TOTAL', natoms, nmolecules)

        return

    def run(self, niterations):
        """
        Run atomtype sampler for the specified number of iterations.

        Parameters
        ----------
        niterations : int
            The specified number of iterations

        """

        for iteration in range(niterations):
            if self.verbose:
                print("Iteration %d / %d" % (iteration, niterations))

            accepted = self.sample_atomtypes()

            if self.verbose:
                if accepted:
                    print('Accepted.')
                else:
                    print('Rejected.')

                # Compute atomtype statistics on molecules.
                [atom_typecounts, molecule_typecounts] = self.compute_type_statistics(self.atomtypes, self.molecules)
                self.show_type_statistics(self.atomtypes, atom_typecounts, molecule_typecounts, atomtype_matches=self.atom_type_matches)

                print('')
