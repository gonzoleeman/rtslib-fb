'''
Implements the RTSRoot class.

This file is part of RTSLib.
Copyright (c) 2011-2013 by Datera, Inc

Licensed under the Apache License, Version 2.0 (the "License"); you may
not use this file except in compliance with the License. You may obtain
a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
License for the specific language governing permissions and limitations
under the License.
'''

import re
import os

from node import CFSNode
from target import Target
from fabric import FabricModule
from tcm import (StorageObject, FileIOStorageObject, BlockStorageObject,
                 PSCSIStorageObject, RDMCPStorageObject)
from utils import RTSLibError, RTSLibBrokenLink, modprobe
from utils import dict_remove, set_attributes

storageobjects = dict(
    fileio=FileIOStorageObject,
    block=BlockStorageObject,
    pscsi=PSCSIStorageObject,
    ramdisk=RDMCPStorageObject,
    )

class RTSRoot(CFSNode):
    '''
    This is an interface to the root of the configFS object tree.
    Is allows one to start browsing Target and StorageObjects,
    as well as helper methods to return arbitrary objects from the
    configFS tree.

    >>> import rtslib.root as root
    >>> rtsroot = root.RTSRoot()
    >>> rtsroot.path
    '/sys/kernel/config/target'
    >>> rtsroot.exists
    True
    >>> rtsroot.targets # doctest: +ELLIPSIS
    [...]
    >>> rtsroot.tpgs # doctest: +ELLIPSIS
    [...]
    >>> rtsroot.storage_objects # doctest: +ELLIPSIS
    [...]
    >>> rtsroot.network_portals # doctest: +ELLIPSIS
    [...]

    '''

    # RTSRoot private stuff
    def __init__(self):
        '''
        Instantiate an RTSRoot object. Basically checks for configfs setup and
        base kernel modules (tcm)
        '''
        super(RTSRoot, self).__init__()
        modprobe('target_core_mod')
        self._create_in_cfs_ine('any')

    def _list_targets(self):
        self._check_self()
        for fabric_module in self.fabric_modules:
            for target in fabric_module.targets:
                yield target

    def _list_storage_objects(self):
        self._check_self()
        for so in StorageObject.all():
            yield so

    def _list_tpgs(self):
        self._check_self()
        for t in self.targets:
            for tpg in t.tpgs:
                yield tpg

    def _list_node_acls(self):
        self._check_self()
        for t in self.tpgs:
            for node_acl in t.node_acls:
                yield node_acl

    def _list_network_portals(self):
        self._check_self()
        for t in self.tpgs:
            for p in t.network_portals:
                yield p

    def _list_luns(self):
        self._check_self()
        for t in self.tpgs:
            for lun in t.luns:
                yield lun

    def _list_sessions(self):
        self._check_self()
        for na in self.node_acls:
            if na.session:
                yield na.session

    def _list_fabric_modules(self):
        self._check_self()
        for mod in FabricModule.all():
            yield mod

    def __str__(self):
        return "rtslib"

    # RTSRoot public stuff

    def dump(self):
        '''
        Returns a dict representing the complete state of the target
        config, suitable for serialization/deserialization, and then
        handing to restore().
        '''
        d = super(RTSRoot, self).dump()
        d['storage_objects'] = [so.dump() for so in self.storage_objects]
        d['targets'] = [t.dump() for t in self.targets]
        d['fabric_modules'] = [f.dump() for f in self.fabric_modules
                               if f.has_feature("discovery_auth")
                               if f.discovery_enable_auth]
        return d

    def clear_existing(self, confirm=False):
        '''
        Remove entire current configuration.
        '''
        if not confirm:
            raise RTSLibError("As a precaution, confirm=True needs to be set")

        # Targets depend on storage objects, delete them first.
        for t in self.targets:
            t.delete()
        for fm in (f for f in self.fabric_modules if f.has_feature("discovery_auth")):
            fm.clear_discovery_auth_settings()
        for so in self.storage_objects:
            so.delete()

    def restore(self, config, clear_existing=False):
        '''
        Takes a dict generated by dump() and reconfigures the target to match.
        Returns list of non-fatal errors that were encountered.
        '''
        if clear_existing:
            self.clear_existing(confirm=True)
        elif list(self.storage_objects) or list(self.targets):
            raise RTSLibError("storageobjects or targets present, not restoring." +
                              " Set clear_existing=True?")

        errors = []

        def err_func(err_str):
            errors.append(err_str + ", skipped")

        for index, so in enumerate(config.get('storage_objects', [])):
            if 'name' not in so:
                err_func("'name' not defined in storage object %d" % index)
                continue
            try:
                so_cls = storageobjects[so['plugin']]
            except KeyError:
                err_func("'plugin' not defined or invalid in storageobject %s" % so['name'])
                continue
            kwargs = so.copy()
            dict_remove(kwargs, ('exists', 'attributes', 'plugin', 'buffered_mode'))
            try:
                so_obj = so_cls(**kwargs)
            except (TypeError, ValueError):
                err_func("Could not create StorageObject %s" % so['name'])
                continue
            try:
                set_attributes(so_obj, so.get('attributes', {}))
            except RTSLibError:
                err_func("Could not set an attribute for %s" % so['name'])

        # Don't need to create fabric modules
        for index, fm in enumerate(config.get('fabric_modules', [])):
            if 'name' not in fm:
                err_func("'name' not defined in fabricmodule %d" % index)
                continue
            for fm_obj in self.fabric_modules:
                if fm['name'] == fm_obj.name:
                    fm_obj.setup(fm, err_func)
                    break

        for index, t in enumerate(config.get('targets', [])):
            if 'wwn' not in t:
                err_func("'wwn' not defined in target %d" % index)
                continue
            if 'fabric' not in t:
                err_func("target %s missing 'fabric' field" % t['wwn'])
                continue
            if t['fabric'] not in (f.name for f in self.fabric_modules):
                err_func("Unknown fabric '%s'" % t['fabric'])
                continue

            fm_obj = FabricModule(t['fabric'])

            # Instantiate target
            Target.setup(fm_obj, list(self.storage_objects), t, err_func)

        return errors

    targets = property(_list_targets,
            doc="Get the list of Target objects.")
    tpgs = property(_list_tpgs,
            doc="Get the list of all the existing TPG objects.")
    node_acls = property(_list_node_acls,
            doc="Get the list of all the existing NodeACL objects.")
    sessions = property(_list_sessions,
            doc="Get the list of all the existing sessions.")
    network_portals = property(_list_network_portals,
            doc="Get the list of all the existing Network Portal objects.")
    storage_objects = property(_list_storage_objects,
            doc="Get the list of all the existing Storage objects.")
    luns = property(_list_luns,
            doc="Get the list of all existing LUN objects.")
    fabric_modules = property(_list_fabric_modules,
            doc="Get the list of all FabricModule objects.")

def _test():
    '''Run the doctests.'''
    import doctest
    doctest.testmod()

if __name__ == "__main__":
    _test()
