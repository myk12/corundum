# Sync DCN Application (Corundum)

This directory contains a custom Corundum application named **sync_dcn**. It integrates a **synchronous consensus** data plane into the `mqnic` FPGA NIC design and provides RTL, simulation, and software scaffolding. The design targets **time-slotted, synchronous networks** where each node transmits/receives in fixed slots and reaches consensus on per-slot log items.

## Purpose

- Provide an **FPGA-resident consensus pipeline** that runs in lock-step slots using PTP time.
- Route traffic between host DMA and the network while allowing the consensus core to inject/receive packets.
- Offer a starting point for **custom applications** on Corundum with both RTL and Linux/user-space hooks.

## High-Level Functionality

- **Consensus core** processes packets from peers, tracks node health, and produces committed logs per slot.
- **AXI-Stream wrapper** performs frame-aware arbitration between host DMA and consensus traffic, and routes RX frames by EtherType.
- **Corundum application block** integrates the consensus logic with the `mqnic` application interface (AXI-Lite control, DMA, and MAC streams).
- **Simulation testbench** enables functional verification around the PCIe/AXI datapath.

## Directory Layout

```
sync_dcn/
├─ rtl/                 # RTL implementation (Verilog)
├─ tb/                  # Testbench and simulation artifacts
├─ modules/             # Linux kernel modules (app + mqnic driver copy)
├─ utils/               # User-space utilities and libraries
└─ lib/                 # Shared IP libraries (axis/eth/pcie/psmake)
```

## Key RTL Modules

### Application Block Integration
- **mqnic_app_block_sync_dcn.v**
  - Corundum application block integration point.
  - Connects AXI-Lite control, DMA descriptors, and MAC AXIS ports.
  - Instantiates and wires the sync DCN application logic.

### Consensus Logic
- **consensus_core.v**
  - Implements the core state machine for slot-based consensus.
  - Tracks knowledge vectors, alive mask, and commit decisions.
  - Produces committed log items and health status.

- **consensus_node.v**
  - Node-level wrapper around the consensus core.
  - Manages timing (slot boundaries) and packet formatting.
  - Interfaces to MAC AXIS streams for TX/RX.

### AXI-Stream Wrapper
- **consensus_app_wrapper.v**
  - Frame-aware arbiter for TX: **core traffic has priority**, switch only on frame boundaries.
  - RX routing based on EtherType (default 0x88B5 for consensus frames).
  - Bypass mode when disabled: Host DMA ↔ MAC directly.

### Other RTL Support
- **consensus_rx.v / consensus_tx.v / consensus_scheduler.v**
  - RX parsing, TX framing, and slot scheduling helpers.

## Software Components

### Kernel Modules
- **modules/mqnic**
  - Copy of the `mqnic` driver sources used for local integration and builds.
- **modules/mqnic_app_template**
  - Template auxiliary driver that binds to an application ID and accesses app registers.

### User-Space Utilities
- **utils/app-template-test.c**
  - Simple test app to open `/dev/mqnicX`, check app ID, and perform a register read/write.

## Configuration Notes

### Top-level (Wrapper) Parameters
- **`P_CONSENSUS_ETHERTYPE`**: EtherType for consensus frames (default `0x88B5`).
- **`P_HDR_ETHERTYPE_OFFSET_BYTES`**: Byte offset of EtherType field in Ethernet frame (default `12`).
- **`P_SLOT_DURATION_NS` / `P_GUARD_BAND_NS` / `P_COMMIT_TIME_NS`**: Slot timing in nanoseconds.
- **`P_LOG_ITEM_LEN`**: Log item payload length (bytes), default `40`.
- **`P_NODE_MAC_ADDR`**: Source MAC address for consensus TX.
- **`P_NODE_ID_WIDTH`** / **`P_KV_WIDTH`**: Field widths for node ID and knowledge vector.
- **`P_HDR_SLOT_ID_OFFSET` / `P_HDR_NODE_ID_OFFSET` / `P_HDR_KV_OFFSET` / `P_HDR_PAYLOAD_OFFSET`**: Header layout byte offsets.
- **`P_DEST_MAC_0..4` / `P_BROADCAST_MAC`**: Destination MAC lookup entries and broadcast MAC.

### Node / TX / RX / Core Parameters (propagated via `consensus_node`)
- **`P_NODE_ID` / `P_NODE_COUNT`**: Cluster configuration.
- **`P_ETHERNET_TYPE`**: Consensus EtherType (wired from wrapper by default).
- **`P_NODE_ID_WIDTH` / `P_KV_WIDTH`**: Field widths.
- **Header Offsets**: `P_HDR_ETHERTYPE_OFFSET`, `P_HDR_SLOT_ID_OFFSET`, `P_HDR_NODE_ID_OFFSET`, `P_HDR_KV_OFFSET`, `P_HDR_PAYLOAD_OFFSET`.
- **MAC Table**: `P_DEST_MAC_0..4`, `P_BROADCAST_MAC`.
- **`P_LOG_ITEM_LEN`**: Payload length; RX/TX use this to size fields and endian swap.
- **Core Quorum**: `P_CONSENSUS_QUORUM` (default majority) and derived counter widths.

### Scheduler Parameters
- **`P_TX_NODE_SPACING_NS`**: Per-node TX start spacing (default `200 ns`).
- **`P_SLOT_DURATION_NS` / `P_GUARD_NS` / `P_COMMIT_DURATION_NS`**: Slot timing.

### PTP & Time Alignment
- PTP ToD (96-bit) is mapped to a 64-bit nanoseconds value for slot alignment.
- Wrapper checks `PTP_TS_WIDTH` is 96; if you use a different format, add a mapper.

## Simulation

The testbench under [tb/mqnic_core_pcie_us](tb/mqnic_core_pcie_us) provides a PCIe/AXI-based simulation environment and Python tests for functional validation. Build artifacts and waves are also placed there during simulation.

## How to Extend

- Adjust header offsets and field widths to support VLAN/QinQ or alternative encodings.
- Provide a custom destination MAC table via wrapper parameters for your deployment.
- Modify or extend **consensus_core.v** to change the consensus algorithm or quorum rule.
- Add new application registers in **mqnic_app_block_sync_dcn.v** for control/status.
- Update the **consensus_app_wrapper.v** RX routing policy if you introduce new frame types.
- Implement a custom user-space or kernel driver using the template as a starting point.

## Notes & Caveats

- This application assumes **synchronous networking** with deterministic slot timing.
- Frame arbitration is **non-preemptive** (no interleaving mid-frame).
- The current host interface in the wrapper is tied off; add host interaction paths if needed.

---

If you need a deeper walkthrough (e.g., a complete signal-level data path or timing diagram), request it and specify the exact module or flow.
