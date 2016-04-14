#  This file is part of the myhdl library, a Python package for using
#  Python as a Hardware Description Language.
#
#  Copyright (C) 2003-2016 Jan Decaluwe
#
#  The myhdl library is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public License as
#  published by the Free Software Foundation; either version 2.1 of the
#  License, or (at your option) any later version.
#
#  This library is distributed in the hope that it will be useful, but
#  WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#  Lesser General Public License for more details.

#  You should have received a copy of the GNU Lesser General Public
#  License along with this library; if not, write to the Free Software
#  Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA

""" Block with the @block decorator function. """
from __future__ import absolute_import

import inspect

from functools import wraps

import myhdl
from myhdl import BlockError, BlockInstanceError, Cosimulation
from myhdl._instance import _Instantiator
from myhdl._util import _flatten
from myhdl._extractHierarchy import (_makeMemInfo,
                                     _UserVerilogCode, _UserVhdlCode)
from myhdl._Signal import _Signal, _isListOfSigs


class _error:
    pass
_error.ArgType = "%s: A block should return block or instantiator objects"
_error.InstanceError = "%s: subblock %s should be encapsulated in a block decorator"


class _CallInfo(object):

    def __init__(self, name, modctxt, symdict):
        self.name = name
        self.modctxt = modctxt
        self.symdict = symdict


def _getCallInfo():
    """Get info on the caller of a BlockInstance.

    A BlockInstance should be used in a block context.
    This function gets the required info from the caller
    It uses the frame stack:
    0: this function
    1: block instance constructor
    2: the decorator function call
    3: the function that defines instances
    4: the caller of the block function, e.g. a BlockInstance.

    """

    stack = inspect.stack()
    # caller may be undefined if instantiation from a Python module
    callerrec = None
    funcrec = stack[3]
    if len(stack) > 4:
        callerrec = stack[4]

    name = funcrec[3]
    frame = funcrec[0]
    symdict = dict(frame.f_globals)
    symdict.update(frame.f_locals)
    modctxt = False
    if callerrec is not None:
        f_locals = callerrec[0].f_locals
        if 'self' in f_locals:
            modctxt = isinstance(f_locals['self'], _Block)
    return _CallInfo(name, modctxt, symdict)


def block(func):
    srcfile = inspect.getsourcefile(func)
    srcline = inspect.getsourcelines(func)[0]

    @wraps(func)
    def deco(*args, **kwargs):
        deco.calls += 1
        return _Block(func, deco, srcfile, srcline, *args, **kwargs)
    deco.calls = 0
    return deco


class _Block(object):

    def __init__(self, func, deco, srcfile, srcline, *args, **kwargs):
        calls = deco.calls
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.__doc__ = func.__doc__
        callinfo = _getCallInfo()
        self.callinfo = callinfo
        self.modctxt = callinfo.modctxt
        self.callername = callinfo.name
        self.symdict = None
        self.sigdict = {}
        self.memdict = {}
        self.name = self.__name__ = func.__name__ + '_' + str(calls - 1)

        # flatten, but keep BlockInstance objects
        self.subs = _flatten(func(*args, **kwargs))
        self._verifySubs()
        self._updateNamespaces()
        self.verilog_code = self.vhdl_code = None
        self.sim = None
        if hasattr(deco, 'verilog_code'):
            self.verilog_code = _UserVerilogCode(deco.verilog_code, self.symdict, func.__name__,
                                                 func, srcfile, srcline)
        if hasattr(deco, 'vhdl_code'):
            self.vhdl_code = _UserVhdlCode(deco.vhdl_code, self.symdict, func.__name__,
                                           func, srcfile, srcline)
        self._config_sim = {'trace': False}

    def _verifySubs(self):
        for inst in self.subs:
            if not isinstance(inst, (_Block, _Instantiator, Cosimulation)):
                raise BlockError(_error.ArgType % (self.name,))
            if isinstance(inst, (_Block, _Instantiator)):
                if not inst.modctxt:
                    raise BlockError(_error.InstanceError % (self.name, inst.callername))

    def _updateNamespaces(self):
        # dicts to keep track of objects used in Instantiator objects
        usedsigdict = {}
        usedlosdict = {}
        for inst in self.subs:
            # the symdict of a block instance is defined by
            # the call context of its instantiations
            if isinstance(inst, Cosimulation):
                continue  # ignore
            if self.symdict is None:
                self.symdict = inst.callinfo.symdict
            if isinstance(inst, _Instantiator):
                usedsigdict.update(inst.sigdict)
                usedlosdict.update(inst.losdict)
        if self.symdict is None:
            self.symdict = {}
        # Special case: due to attribute reference transformation, the
        # sigdict and losdict from Instantiator objects may contain new
        # references. Therefore, update the symdict with them.
        # To be revisited.
        self.symdict.update(usedsigdict)
        self.symdict.update(usedlosdict)
        # Infer sigdict and memdict, with compatibility patches from _extractHierarchy
        for n, v in self.symdict.items():
            if isinstance(v, _Signal):
                self.sigdict[n] = v
                if n in usedsigdict:
                    v._markUsed()
            if _isListOfSigs(v):
                m = _makeMemInfo(v)
                self.memdict[n] = m
                if n in usedlosdict:
                    m._used = True

    def _inferInterface(self):
        from myhdl.conversion._analyze import _analyzeTopFunc
        intf = _analyzeTopFunc(self.func, *self.args, **self.kwargs)
        self.argnames = intf.argnames
        self.argdict = intf.argdict

    # Public methods
    # The puropse now is to define the API, optimizations later

    def verify_convert(self):
        return myhdl.conversion.verify(self)

    def analyze_convert(self):
        return myhdl.conversion.analyze(self)

    def convert(self, hdl='Verilog', **kwargs):
        """Converts this BlockInstance to another HDL

        Args:
            hdl (Optional[str]): Target HDL. Defaults to Verilog
            path (Optional[str]): Destination folder. Defaults to current
                working dir.
            name (Optional[str]): Module and output file name. Defaults to
                `self.mod.__name__`
            trace(Optional[bool]): Verilog only. Whether the testbench should
                dump all signal waveforms. Defaults to False.
            tb (Optional[bool]): Verilog only. Specifies whether a testbench
                should be created. Defaults to True.
            timescale(Optional[str]): Verilog only. Defaults to '1ns/10ps'
        """
        if hdl.lower() == 'vhdl':
            converter = myhdl.conversion._toVHDL.toVHDL
        elif hdl.lower() == 'verilog':
            converter = myhdl.conversion._toVerilog.toVerilog
        else:
            raise BlockInstanceError('unknown hdl %s' % hdl)

        conv_attrs = {}
        if 'name' in kwargs:
            conv_attrs['name'] = kwargs.pop('name')
        conv_attrs['directory'] = kwargs.pop('path', '')
        if hdl.lower() == 'verilog':
            conv_attrs['no_testbench'] = not kwargs.pop('tb', True)
            conv_attrs['timescale'] = kwargs.pop('timescale', '1ns/10ps')
            conv_attrs['trace'] = kwargs.pop('trace', False)
        conv_attrs.update(kwargs)
        for k, v in conv_attrs.items():
            setattr(converter, k, v)
        return converter(self)

    def config_sim(self, trace=False):
        self._config_sim['trace'] = trace

    def run_sim(self, duration=None, quiet=0):
        if self.sim is None:
            sim = self
            if self._config_sim['trace']:
                sim = myhdl.traceSignals(self)
            self.sim = myhdl._Simulation.Simulation(sim)
        self.sim.run(duration, quiet)

    def quit_sim(self):
        if self.sim is not None:
            self.sim.quit()