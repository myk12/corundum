#!/usr/bin/env python
# SPDX-License-Identifier: BSD-2-Clause-Views
# Copyright (c) 2020-2023 The Regents of the University of California

import itertools
import logging
import os
import struct

import scapy.utils
from scapy.layers.l2 import Ether
from scapy.layers.inet import IP, UDP

import cocotb_test.simulator
import pytest

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge
from cocotb.regression import TestFactory

from cocotbext.axi import AxiLiteBus, AxiLiteMaster
from cocotbext.axi.stream import define_stream


TxReqBus, TxReqTransaction, TxReqSource, TxReqSink, TxReqMonitor = define_stream("TxReq",
    signals=["queue", "tag", "valid"],
    optional_signals=["ready"]
)


TxStatusBus, TxStatusTransaction, TxStatusSource, TxStatusSink, TxStatusMonitor = define_stream("TxStatus",
    signals=["tag", "valid"],
    optional_signals=["empty", "error", "len", "ready"]
)


DoorbellBus, DoorbellTransaction, DoorbellSource, DoorbellSink, DoorbellMonitor = define_stream("Doorbell",
    signals=["queue", "valid"],
    optional_signals=["ready"]
)


CtrlBus, CtrlTransaction, CtrlSource, CtrlSink, CtrlMonitor = define_stream("Ctrl",
    signals=["queue", "enable", "valid"],
    optional_signals=["ready"]
)


class TB(object):
    def __init__(self, dut):
        self.dut = dut

        self.log = logging.getLogger("cocotb.tb")
        self.log.setLevel(logging.DEBUG)

        cocotb.start_soon(Clock(dut.clk, 4, units="ns").start())

        self.tx_req_sink = TxReqSink(TxReqBus.from_prefix(dut, "m_axis_tx_req"), dut.clk, dut.rst)
        self.tx_status_dequeue_source = TxStatusSource(TxStatusBus.from_prefix(dut, "s_axis_tx_status_dequeue"), dut.clk, dut.rst)
        self.tx_status_start_source = TxStatusSource(TxStatusBus.from_prefix(dut, "s_axis_tx_status_start"), dut.clk, dut.rst)
        self.tx_status_finish_source = TxStatusSource(TxStatusBus.from_prefix(dut, "s_axis_tx_status_finish"), dut.clk, dut.rst)

        self.doorbell_source = DoorbellSource(DoorbellBus.from_prefix(dut, "s_axis_doorbell"), dut.clk, dut.rst)

        self.ctrl_source = CtrlSource(CtrlBus.from_prefix(dut, "s_axis_sched_ctrl"), dut.clk, dut.rst)

        self.axil_master = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "s_axil"), dut.clk, dut.rst)

        dut.enable.setimmediatevalue(0)

    def set_idle_generator(self, generator=None):
        if generator:
            self.tx_status_dequeue_source.set_pause_generator(generator())
            self.tx_status_start_source.set_pause_generator(generator())
            self.tx_status_finish_source.set_pause_generator(generator())

    def set_backpressure_generator(self, generator=None):
        if generator:
            self.tx_req_sink.set_pause_generator(generator())

    async def reset(self):
        self.dut.rst.setimmediatevalue(0)
        await RisingEdge(self.dut.clk)
        await RisingEdge(self.dut.clk)
        self.dut.rst.value = 1
        await RisingEdge(self.dut.clk)
        await RisingEdge(self.dut.clk)
        self.dut.rst.value = 0
        await RisingEdge(self.dut.clk)
        await RisingEdge(self.dut.clk)


async def run_test_config(dut):

    tb = TB(dut)

    await tb.reset()

    assert await tb.axil_master.read_dword(0*4) == 0

    await tb.axil_master.write_dword(0*4, 3)

    assert await tb.axil_master.read_dword(0*4) == 3

    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)


async def run_test_single(dut, idle_inserter=None, backpressure_inserter=None):

    tb = TB(dut)

    await tb.reset()

    tb.set_idle_generator(idle_inserter)
    tb.set_backpressure_generator(backpressure_inserter)

    dut.enable.value = 1

    await tb.axil_master.write_dword(0*4, 3)

    await tb.doorbell_source.send(DoorbellTransaction(queue=0))

    for k in range(200):
        await RisingEdge(dut.clk)

    for k in range(10):
        tx_req = await tb.tx_req_sink.recv()
        tb.log.info("TX request: %s", tx_req)

        assert tx_req.queue == 0

        status = TxStatusTransaction(empty=0, error=0, len=1000, tag=tx_req.tag)
        tb.log.info("TX status: %s", status)
        await tb.tx_status_dequeue_source.send(status)
        await tb.tx_status_start_source.send(status)
        await tb.tx_status_finish_source.send(status)

    tx_req = await tb.tx_req_sink.recv()
    tb.log.info("TX request: %s", tx_req)

    assert tx_req.queue == 0

    status = TxStatusTransaction(empty=1, error=0, len=0, tag=tx_req.tag)
    tb.log.info("TX status: %s", status)
    await tb.tx_status_dequeue_source.send(status)

    for k in range(200):
        await RisingEdge(dut.clk)

    while not tb.tx_req_sink.empty():
        tx_req = await tb.tx_req_sink.recv()
        tb.log.info("TX request: %s", tx_req)

        assert tx_req.queue == 0

        status = TxStatusTransaction(empty=1, error=0, len=0, tag=tx_req.tag)
        tb.log.info("TX status: %s", status)
        await tb.tx_status_dequeue_source.send(status)

    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)


async def run_test_multiple(dut, idle_inserter=None, backpressure_inserter=None):

    tb = TB(dut)

    await tb.reset()

    tb.set_idle_generator(idle_inserter)
    tb.set_backpressure_generator(backpressure_inserter)

    dut.enable.value = 1

    for k in range(10):
        await tb.axil_master.write_dword(k*4, 3)

    for k in range(10):
        await tb.doorbell_source.send(DoorbellTransaction(queue=k))

    for k in range(200):
        await RisingEdge(dut.clk)

    for k in range(100):
        tx_req = await tb.tx_req_sink.recv()
        tb.log.info("TX request: %s", tx_req)

        assert tx_req.queue == k % 10

        status = TxStatusTransaction(empty=0, error=0, len=1000, tag=tx_req.tag)
        tb.log.info("TX status: %s", status)
        await tb.tx_status_dequeue_source.send(status)
        await tb.tx_status_start_source.send(status)
        await tb.tx_status_finish_source.send(status)

    for k in range(10):
        tx_req = await tb.tx_req_sink.recv()
        tb.log.info("TX request: %s", tx_req)

        status = TxStatusTransaction(empty=1, error=0, len=0, tag=tx_req.tag)
        tb.log.info("TX status: %s", status)
        await tb.tx_status_dequeue_source.send(status)

    for k in range(200):
        await RisingEdge(dut.clk)

    while not tb.tx_req_sink.empty():
        tx_req = await tb.tx_req_sink.recv()
        tb.log.info("TX request: %s", tx_req)

        status = TxStatusTransaction(empty=1, error=0, len=0, tag=tx_req.tag)
        tb.log.info("TX status: %s", status)
        await tb.tx_status_dequeue_source.send(status)

    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)


async def run_test_doorbell(dut, idle_inserter=None, backpressure_inserter=None):

    tb = TB(dut)

    await tb.reset()

    tb.set_idle_generator(idle_inserter)
    tb.set_backpressure_generator(backpressure_inserter)

    dut.enable.value = 1

    await tb.axil_master.write_dword(0*4, 3)

    await tb.doorbell_source.send(DoorbellTransaction(queue=0))

    for k in range(200):
        await RisingEdge(dut.clk)

    for k in range(10):
        tx_req = await tb.tx_req_sink.recv()
        tb.log.info("TX request: %s", tx_req)

        assert tx_req.queue == 0

        status = TxStatusTransaction(empty=0, error=0, len=1000, tag=tx_req.tag)
        tb.log.info("TX status: %s", status)
        await tb.tx_status_dequeue_source.send(status)
        await tb.tx_status_start_source.send(status)
        await tb.tx_status_finish_source.send(status)

    for k in range(200):
        await RisingEdge(dut.clk)

    tx_req = await tb.tx_req_sink.recv()
    tb.log.info("TX request: %s", tx_req)

    assert tx_req.queue == 0

    status = TxStatusTransaction(empty=1, error=0, len=0, tag=tx_req.tag)
    tb.log.info("TX status: %s", status)
    await tb.tx_status_dequeue_source.send(status)

    await tb.doorbell_source.send(DoorbellTransaction(queue=0))

    tx_req = await tb.tx_req_sink.recv()
    tb.log.info("TX request: %s", tx_req)

    assert tx_req.queue == 0

    status = TxStatusTransaction(empty=1, error=0, len=0, tag=tx_req.tag)
    tb.log.info("TX status: %s", status)
    await tb.tx_status_dequeue_source.send(status)

    tx_req = await tb.tx_req_sink.recv()
    tb.log.info("TX request: %s", tx_req)

    assert tx_req.queue == 0

    status = TxStatusTransaction(empty=1, error=0, len=0, tag=tx_req.tag)
    tb.log.info("TX status: %s", status)
    await tb.tx_status_dequeue_source.send(status)

    for k in range(10):
        tx_req = await tb.tx_req_sink.recv()
        tb.log.info("TX request: %s", tx_req)

        assert tx_req.queue == 0

        status = TxStatusTransaction(empty=0, error=0, len=1000, tag=tx_req.tag)
        tb.log.info("TX status: %s", status)
        await tb.tx_status_dequeue_source.send(status)
        await tb.tx_status_start_source.send(status)
        await tb.tx_status_finish_source.send(status)

    for k in range(200):
        await RisingEdge(dut.clk)

    tx_req = await tb.tx_req_sink.recv()
    tb.log.info("TX request: %s", tx_req)

    assert tx_req.queue == 0

    status = TxStatusTransaction(empty=1, error=0, len=0, tag=tx_req.tag)
    tb.log.info("TX status: %s", status)
    await tb.tx_status_dequeue_source.send(status)

    for k in range(200):
        await RisingEdge(dut.clk)

    while not tb.tx_req_sink.empty():
        tx_req = await tb.tx_req_sink.recv()
        tb.log.info("TX request: %s", tx_req)

        assert tx_req.queue == 0

        status = TxStatusTransaction(empty=1, error=0, len=0, tag=tx_req.tag)
        tb.log.info("TX status: %s", status)
        await tb.tx_status_dequeue_source.send(status)

    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)


def cycle_pause():
    return itertools.cycle([1, 1, 1, 0])


if cocotb.SIM_NAME:

    factory = TestFactory(run_test_config)
    factory.generate_tests()

    for test in [
                run_test_single,
                run_test_multiple,
                run_test_doorbell
            ]:

        factory = TestFactory(test)
        factory.add_option("idle_inserter", [None, cycle_pause])
        factory.add_option("backpressure_inserter", [None, cycle_pause])
        factory.generate_tests()


# cocotb-test

tests_dir = os.path.dirname(__file__)
rtl_dir = os.path.abspath(os.path.join(tests_dir, '..', '..', 'rtl'))
lib_dir = os.path.abspath(os.path.join(rtl_dir, '..', 'lib'))
axi_rtl_dir = os.path.abspath(os.path.join(lib_dir, 'axi', 'rtl'))
axis_rtl_dir = os.path.abspath(os.path.join(lib_dir, 'axis', 'rtl'))
eth_rtl_dir = os.path.abspath(os.path.join(lib_dir, 'eth', 'rtl'))
pcie_rtl_dir = os.path.abspath(os.path.join(lib_dir, 'pcie', 'rtl'))


def test_tx_scheduler_rr(request):
    dut = "tx_scheduler_rr"
    module = os.path.splitext(os.path.basename(__file__))[0]
    toplevel = dut

    verilog_sources = [
        os.path.join(rtl_dir, f"{dut}.v"),
        os.path.join(axis_rtl_dir, "axis_fifo.v"),
        os.path.join(axis_rtl_dir, "priority_encoder.v"),
    ]

    parameters = {}

    parameters['AXIL_DATA_WIDTH'] = 32
    parameters['AXIL_ADDR_WIDTH'] = 16
    parameters['AXIL_STRB_WIDTH'] = parameters['AXIL_DATA_WIDTH'] // 8
    parameters['LEN_WIDTH'] = 16
    parameters['REQ_TAG_WIDTH'] = 8
    parameters['OP_TABLE_SIZE'] = 16
    parameters['QUEUE_INDEX_WIDTH'] = 6
    parameters['PIPELINE'] = 2
    parameters['SCHED_CTRL_ENABLE'] = 1

    extra_env = {f'PARAM_{k}': str(v) for k, v in parameters.items()}

    sim_build = os.path.join(tests_dir, "sim_build",
        request.node.name.replace('[', '-').replace(']', ''))

    cocotb_test.simulator.run(
        python_search=[tests_dir],
        verilog_sources=verilog_sources,
        toplevel=toplevel,
        module=module,
        parameters=parameters,
        sim_build=sim_build,
        extra_env=extra_env,
    )
