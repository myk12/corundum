`timescale 1ns / 1ps
`default_nettype none

/*
 * Consensus App Wrapper for Corundum
 *
 * Clean AXI-stream integration wrapper:
 * - TX: 2-input arbiter (Core has priority, but switches only on frame boundaries)
 * - RX: per-frame routing based on EtherType (0x88B5) latched on first beat handshake
 * - When i_enable=0: strict bypass (Host DMA <-> MAC), core interfaces held idle
 *
 * Notes:
 * - This wrapper does NOT generate/modify mqnic metadata; it only routes the existing AXIS streams.
 * - All combinational outputs have complete default assignments (no latches).
 * - No implicit nets allowed.
 */

module consensus_app_wrapper #(
    parameter               P_NODE_ID       = 0,
    parameter               PTP_TS_WIDTH    = 96,

    // Interface Configuration
    parameter               AXIS_DATA_WIDTH     = 512,
    parameter               AXIS_KEEP_WIDTH     = AXIS_DATA_WIDTH/8,
    parameter               AXIS_TX_USER_WIDTH  = 1,
    parameter               AXIS_RX_USER_WIDTH  = 1,
    parameter               AXIS_USER_WIDTH     = AXIS_TX_USER_WIDTH, // For consensus core
    parameter               TX_TAG_WIDTH        = 16,

    // Consensus routing parameters
    parameter [15:0]        P_CONSENSUS_ETHERTYPE           = 16'h88B5,
    parameter integer       P_HDR_ETHERTYPE_OFFSET_BYTES    = 12,
    // Consensus configuration parameters (propagated to node)
    parameter integer       P_SLOT_DURATION_NS              = 10000,
    parameter integer       P_GUARD_BAND_NS                 = 1000,
    parameter integer       P_COMMIT_TIME_NS               = 1000,
    parameter integer       P_LOG_ITEM_LEN                 = 40,
    parameter [47:0]        P_NODE_MAC_ADDR                = 48'h00_0a_35_06_50_94,
    parameter integer       P_NODE_ID_WIDTH                = 8,
    parameter integer       P_KV_WIDTH                     = 8,
    parameter integer       P_HDR_SLOT_ID_OFFSET           = 14,
    parameter integer       P_HDR_NODE_ID_OFFSET           = 22,
    parameter integer       P_HDR_KV_OFFSET                = 23,
    parameter integer       P_HDR_PAYLOAD_OFFSET           = 24,
    parameter [47:0]        P_DEST_MAC_0                   = 48'h00_0a_35_06_50_94,
    parameter [47:0]        P_DEST_MAC_1                   = 48'h00_0a_35_06_09_24,
    parameter [47:0]        P_DEST_MAC_2                   = 48'h00_0a_35_06_0b_84,
    parameter [47:0]        P_DEST_MAC_3                   = 48'h00_0a_35_06_09_3c,
    parameter [47:0]        P_DEST_MAC_4                   = 48'h00_0a_35_06_0b_72,
    parameter [47:0]        P_BROADCAST_MAC                = 48'hFF_FF_FF_FF_FF_FF
)(
    // --------------------------------------------------------
    // 0. Global Signals
    // --------------------------------------------------------
    input  wire                                 clk,
    input  wire                                 rst,

    input  wire                                 i_enable,

    // --------------------------------------------------------
    // 1. PTP Time Input (from Corundum PTP Hardware Clock)
    // --------------------------------------------------------
    input  wire                                 ptp_clk,
    input  wire                                 ptp_rst,
    input  wire [PTP_TS_WIDTH-1:0]              ptp_sync_ts_tod,

    // --------------------------------------------------------
    // 2. TX Path (Host DMA -> APP Wrapper -> MAC)
    // --------------------------------------------------------
    // AXI Stream from Host DMA to Wrapper
    input  wire [AXIS_DATA_WIDTH-1:0]           s_axis_dma_tx_tdata,
    input  wire [AXIS_KEEP_WIDTH-1:0]           s_axis_dma_tx_tkeep,
    input  wire                                 s_axis_dma_tx_tvalid,
    input  wire                                 s_axis_dma_tx_tlast,
    input  wire [AXIS_TX_USER_WIDTH-1:0]        s_axis_dma_tx_tuser,
    output reg                                  s_axis_dma_tx_tready,

    // AXI Stream from Wrapper to MAC (arbiter output)
    output reg  [AXIS_DATA_WIDTH-1:0]           m_axis_mac_tx_tdata,
    output reg  [AXIS_KEEP_WIDTH-1:0]           m_axis_mac_tx_tkeep,
    output reg                                  m_axis_mac_tx_tvalid,
    output reg                                  m_axis_mac_tx_tlast,
    output reg  [AXIS_TX_USER_WIDTH-1:0]        m_axis_mac_tx_tuser,
    input  wire                                 m_axis_mac_tx_tready,

    // --------------------------------------------------------
    // 3. RX Path (MAC -> Host DMA)
    // --------------------------------------------------------
    // AXI Stream from MAC to Wrapper
    input  wire [AXIS_DATA_WIDTH-1:0]           s_axis_mac_rx_tdata,
    input  wire [AXIS_KEEP_WIDTH-1:0]           s_axis_mac_rx_tkeep,
    input  wire                                 s_axis_mac_rx_tvalid,
    input  wire                                 s_axis_mac_rx_tlast,
    input  wire [AXIS_RX_USER_WIDTH-1:0]        s_axis_mac_rx_tuser,
    output reg                                  s_axis_mac_rx_tready,

    output reg  [AXIS_DATA_WIDTH-1:0]           m_axis_dma_rx_tdata,
    output reg  [AXIS_KEEP_WIDTH-1:0]           m_axis_dma_rx_tkeep,
    output reg                                  m_axis_dma_rx_tvalid,
    output reg                                  m_axis_dma_rx_tlast,
    output reg  [AXIS_RX_USER_WIDTH-1:0]        m_axis_dma_rx_tuser,
    input  wire                                 m_axis_dma_rx_tready
);

    // Parameter checks (simulation-time)
    initial begin
        if (PTP_TS_WIDTH != 96) begin
            $error("consensus_app_wrapper: PTP_TS_WIDTH must be 96 (ToD format)");
        end
    end

    // =========================================================================
    // Part A: PTP mapping for consensus core
    // =========================================================================
    // [95:48] sec, [47:16] ns, [15:0] frac
    // For now: provide 64-bit value with ns field in low bits (good enough for slot alignment)
    wire [63:0] w_core_ptp_ns = {32'b0, ptp_sync_ts_tod[47:16]};

    // =========================================================================
    // Part B: Consensus node instantiation (host interface tied off)
    // =========================================================================

    // Core TX (core -> wrapper)
    wire [AXIS_DATA_WIDTH-1:0]                  s_axis_core_tx_tdata;
    wire [AXIS_KEEP_WIDTH-1:0]                  s_axis_core_tx_tkeep;
    wire                                        s_axis_core_tx_tvalid;
    wire                                        s_axis_core_tx_tlast;
    wire [AXIS_TX_USER_WIDTH-1:0]               s_axis_core_tx_tuser;
    reg                                         s_axis_core_tx_tready;   // wrapper -> core

    // Core RX (wrapper -> core)
    reg  [AXIS_DATA_WIDTH-1:0]                  m_axis_core_rx_tdata;
    reg  [AXIS_KEEP_WIDTH-1:0]                  m_axis_core_rx_tkeep;
    reg                                         m_axis_core_rx_tvalid;
    reg                                         m_axis_core_rx_tlast;
    reg  [AXIS_RX_USER_WIDTH-1:0]               m_axis_core_rx_tuser;
    wire                                        m_axis_core_rx_tready;   // core -> wrapper

    // Host interface (currently unused) - explicit ties (no implicit nets)
    wire [AXIS_DATA_WIDTH-1:0]                  s_axis_host_req_data        = {AXIS_DATA_WIDTH{1'b0}};
    wire [AXIS_KEEP_WIDTH-1:0]                  s_axis_host_req_keep        = {AXIS_KEEP_WIDTH{1'b0}};
    wire                                        s_axis_host_req_valid       = 1'b0;
    wire                                        s_axis_host_req_last        = 1'b0;
    wire                                        s_axis_host_req_ready;

    wire [AXIS_DATA_WIDTH-1:0]                  m_axis_host_commit_data     = {AXIS_DATA_WIDTH{1'b0}};
    wire [AXIS_KEEP_WIDTH-1:0]                  m_axis_host_commit_keep     = {AXIS_KEEP_WIDTH{1'b0}};
    wire                                        m_axis_host_commit_valid    = 1'b0;
    wire                                        m_axis_host_commit_last     = 1'b0;
    wire                                        m_axis_host_commit_ready;

    consensus_node #(
        // cluster configuration
        .P_NODE_ID(P_NODE_ID),
        .P_NODE_COUNT(3),

        .P_SLOT_DURATION_NS(P_SLOT_DURATION_NS),
        .P_GUARD_BAND_NS(P_GUARD_BAND_NS),
        .P_COMMIT_TIME_NS(P_COMMIT_TIME_NS),

        .P_ETHERNET_TYPE(P_CONSENSUS_ETHERTYPE),
        .P_NODE_MAC_ADDR(P_NODE_MAC_ADDR),
        .P_LOG_ITEM_LEN(P_LOG_ITEM_LEN),

        .P_AXIS_DATA_WIDTH(AXIS_DATA_WIDTH),
        .P_AXIS_KEEP_WIDTH(AXIS_KEEP_WIDTH),
        .P_AXIS_TX_USER_WIDTH(AXIS_TX_USER_WIDTH),
        .P_AXIS_RX_USER_WIDTH(AXIS_RX_USER_WIDTH),

        .P_NODE_ID_WIDTH(P_NODE_ID_WIDTH),
        .P_KV_WIDTH(P_KV_WIDTH),
        .P_HDR_ETHERTYPE_OFFSET(P_HDR_ETHERTYPE_OFFSET_BYTES),
        .P_HDR_SLOT_ID_OFFSET(P_HDR_SLOT_ID_OFFSET),
        .P_HDR_NODE_ID_OFFSET(P_HDR_NODE_ID_OFFSET),
        .P_HDR_KV_OFFSET(P_HDR_KV_OFFSET),
        .P_HDR_PAYLOAD_OFFSET(P_HDR_PAYLOAD_OFFSET),
        .P_DEST_MAC_0(P_DEST_MAC_0),
        .P_DEST_MAC_1(P_DEST_MAC_1),
        .P_DEST_MAC_2(P_DEST_MAC_2),
        .P_DEST_MAC_3(P_DEST_MAC_3),
        .P_DEST_MAC_4(P_DEST_MAC_4),
        .P_BROADCAST_MAC(P_BROADCAST_MAC)
    ) consensus_node_inst (
        .clk(clk),
        .rst_n(!rst),

        .i_enable(i_enable),
        .i_ptp_time_ns(w_core_ptp_ns),

        //---------------------------------------------------------
        // Network interface (MAC)
        //---------------------------------------------------------
        // Core TX (data from consensus core to wrapper)
        .m_axis_mac_tx_tdata(s_axis_core_tx_tdata),
        .m_axis_mac_tx_tkeep(s_axis_core_tx_tkeep),
        .m_axis_mac_tx_tvalid(s_axis_core_tx_tvalid),
        .m_axis_mac_tx_tlast(s_axis_core_tx_tlast),
        .m_axis_mac_tx_tuser(s_axis_core_tx_tuser),
        .m_axis_mac_tx_tready(s_axis_core_tx_tready),

        // Core RX (data from wrapper to consensus core)
        .s_axis_mac_rx_tdata(m_axis_core_rx_tdata),
        .s_axis_mac_rx_tkeep(m_axis_core_rx_tkeep),
        .s_axis_mac_rx_tvalid(m_axis_core_rx_tvalid),
        .s_axis_mac_rx_tlast(m_axis_core_rx_tlast),
        .s_axis_mac_rx_tuser(m_axis_core_rx_tuser),
        .s_axis_mac_rx_tready(m_axis_core_rx_tready),

        //---------------------------------------------------------
        // Host interface (tied off)
        //---------------------------------------------------------
        // Host request in (tied off)
        .s_axis_host_req_data(s_axis_host_req_data),
        .s_axis_host_req_keep(s_axis_host_req_keep),
        .s_axis_host_req_valid(s_axis_host_req_valid),
        .s_axis_host_req_last(s_axis_host_req_last),
        .s_axis_host_req_ready(s_axis_host_req_ready),

        // Host commit out (ignored for now)
        .m_axis_host_commit_data(m_axis_host_commit_data),
        .m_axis_host_commit_keep(m_axis_host_commit_keep),
        .m_axis_host_commit_valid(m_axis_host_commit_valid),
        .m_axis_host_commit_last(m_axis_host_commit_last),
        .m_axis_host_commit_ready(m_axis_host_commit_ready)
    );

    // =========================================================================
    // Part C: TX path arbiter (frame-aware)
    // =========================================================================
    // There are two sources of TX packets: the consensus core and the host DMA. We need to 
    // arbitrate between them to drive the single MAC output stream. 
    // The arbitration policy is:
    // - When i_enable=0: strict bypass (Host DMA only)
    // - When i_enable=1: core has priority, but we only switch on frame boundaries to avoid
    //                    interleaving packets. This means that if a source starts transmitting
    //                    a frame (tvalid with tlast=0), it will be allowed to finish that frame 
    //                    before we switch to the other source, even if the other source becomes 
    //                    valid in the meantime.

    localparam TX_SEL_HOST = 1'b0;
    localparam TX_SEL_CORE = 1'b1;

    reg tx_sel_reg;
    reg tx_lock_reg; // locks to current source until end of frame

    // Effective selection:
    // - If locked, use tx_sel_reg
    // - If not locked, prioritize core if valid, otherwise host
    wire tx_sel_eff = tx_lock_reg ? tx_sel_reg : (s_axis_core_tx_tvalid ? TX_SEL_CORE : TX_SEL_HOST);

    wire sel_host_eff = (tx_sel_eff == TX_SEL_HOST);
    wire sel_core_eff = (tx_sel_eff == TX_SEL_CORE);

    // Selected source signals
    wire [AXIS_DATA_WIDTH-1:0]              m_axis_app_sel_tx_tdata  = sel_host_eff ? s_axis_dma_tx_tdata  : s_axis_core_tx_tdata;
    wire [AXIS_KEEP_WIDTH-1:0]              m_axis_app_sel_tx_tkeep  = sel_host_eff ? s_axis_dma_tx_tkeep  : s_axis_core_tx_tkeep;
    wire                                    m_axis_app_sel_tx_tvalid = sel_host_eff ? s_axis_dma_tx_tvalid : s_axis_core_tx_tvalid;
    wire                                    m_axis_app_sel_tx_tlast  = sel_host_eff ? s_axis_dma_tx_tlast  : s_axis_core_tx_tlast;
    wire [AXIS_TX_USER_WIDTH-1:0]           m_axis_app_sel_tx_tuser  = sel_host_eff ? s_axis_dma_tx_tuser  : s_axis_core_tx_tuser;

    // TX fire
    wire tx_fire = m_axis_app_sel_tx_tvalid && m_axis_mac_tx_tready;

    // Drive Output
    always @(*) begin
        // defaults TX outputs to zero/idle
        m_axis_mac_tx_tdata  = 'b0;
        m_axis_mac_tx_tkeep  = 'b0;
        m_axis_mac_tx_tvalid = 1'b0;
        m_axis_mac_tx_tlast  = 1'b0;
        m_axis_mac_tx_tuser  = 'b0;

        // backpressure to sources: only ready to the selected source
        s_axis_dma_tx_tready = 1'b0;
        s_axis_core_tx_tready = 1'b0;

        if (!i_enable) begin
            // Strict bypass: host -> MAC
            s_axis_dma_tx_tready = m_axis_mac_tx_tready;
            m_axis_mac_tx_tvalid = s_axis_dma_tx_tvalid;

            if (s_axis_dma_tx_tvalid) begin
                m_axis_mac_tx_tdata  = s_axis_dma_tx_tdata;
                m_axis_mac_tx_tkeep  = s_axis_dma_tx_tkeep;
                m_axis_mac_tx_tlast  = s_axis_dma_tx_tlast;
                m_axis_mac_tx_tuser  = s_axis_dma_tx_tuser;
            end
        end else begin
            // Arbitrate between host and core (core has priority, but only switches on frame boundaries)
            m_axis_mac_tx_tvalid = m_axis_app_sel_tx_tvalid;
            m_axis_mac_tx_tdata  = m_axis_app_sel_tx_tdata;
            m_axis_mac_tx_tkeep  = m_axis_app_sel_tx_tkeep;
            m_axis_mac_tx_tlast  = m_axis_app_sel_tx_tlast;
            m_axis_mac_tx_tuser  = m_axis_app_sel_tx_tuser;

            // Ready only to the selected source
            if (sel_host_eff) begin
                s_axis_dma_tx_tready = m_axis_mac_tx_tready;
                s_axis_core_tx_tready = 1'b0; // Not ready to core when host is selected
            end else begin
                s_axis_dma_tx_tready = 1'b0; // Not ready to host when core is selected
                s_axis_core_tx_tready = m_axis_mac_tx_tready;
            end
        end
    end

    // Locking & Selection Update
    always @(posedge clk) begin
        if (rst || !i_enable) begin
            tx_sel_reg <= TX_SEL_HOST;
            tx_lock_reg <= 1'b0;
        end else begin
            if (!tx_lock_reg) begin
                if (s_axis_core_tx_tvalid || s_axis_dma_tx_tvalid) begin
                    tx_sel_reg <= s_axis_core_tx_tvalid ? TX_SEL_CORE : TX_SEL_HOST;
                    tx_lock_reg <= 1'b1; // Lock to this source for the duration of the frame

                    if (m_axis_mac_tx_tready && m_axis_app_sel_tx_tvalid && m_axis_app_sel_tx_tlast) begin
                        tx_lock_reg <= 1'b0; // Frame ends, release lock
                    end
                end
            end else begin
                // Currently locked, check for frame end to release
                if (m_axis_mac_tx_tready && m_axis_app_sel_tx_tvalid && m_axis_app_sel_tx_tlast) begin
                    tx_lock_reg <= 1'b0; // Frame ends, release lock
                end
            end
        end
    end

    // =========================================================================
    // Part D: RX path routing (frame-aware)
    // =========================================================================
    // RX packets are routed based on EtherType (0x88B5) latched on the first beat handshake.
    // If the first beat of a frame matches the EtherType, the entire frame is routed to the 
    // consensus core; otherwise, it's routed to the host DMA. This ensures that frames are 
    // not interleaved and that routing decisions are consistent for each frame.
    
    // EtherType match based on configured header offset (big endian)
    // NOTE: assumes the MAC stream starts at Ethernet destination MAC.
    wire ethertype_match = s_axis_mac_rx_tvalid &&
                       (s_axis_mac_rx_tdata[P_HDR_ETHERTYPE_OFFSET_BYTES*8 +: 16] === {P_CONSENSUS_ETHERTYPE[7:0], P_CONSENSUS_ETHERTYPE[15:8]});

    reg  rx_active_reg;
    reg  rx_route_core_reg;

    // We only decide route on the first beat handshake (tvalid && tready)
    wire rx_fire = s_axis_mac_rx_tvalid && s_axis_mac_rx_tready;

    always @(posedge clk) begin
        if (rst) begin
            rx_active_reg     <= 1'b0;
            rx_route_core_reg <= 1'b0;
        end else begin
            if (!rx_active_reg) begin
                // Start of frame when first beat is accepted
                if (rx_fire) begin
                    rx_active_reg     <= !s_axis_mac_rx_tlast; // Single beat frame, go back to idle
                    rx_route_core_reg <= ethertype_match;       // Frame head locked, route decision made
                end
            end else begin
                // End of frame when last beat is accepted
                if (rx_fire && s_axis_mac_rx_tlast) begin
                    rx_active_reg <= 1'b0;
                    rx_route_core_reg <= 1'b0;
                end
            end
        end
    end

    // Effective route decision
    wire route_to_core_eff = (rx_active_reg ? rx_route_core_reg : ethertype_match);

    always @(*) begin
        // Defaults
        m_axis_dma_rx_tdata  = 'b0;
        m_axis_dma_rx_tkeep  = 'b0;
        m_axis_dma_rx_tvalid = 1'b0;
        m_axis_dma_rx_tlast  = 1'b0;
        m_axis_dma_rx_tuser  = 'b0;

        m_axis_core_rx_tdata  = 'b0;
        m_axis_core_rx_tkeep  = 'b0;
        m_axis_core_rx_tvalid = 1'b0;
        m_axis_core_rx_tlast  = 1'b0;
        m_axis_core_rx_tuser  = 'b0;

        // Tell MAC we're not ready by default (no backpressure)
        // We'll selectively override this to route to the correct destination
        s_axis_mac_rx_tready = 1'b0;

        if (route_to_core_eff) begin
            // Route to consensus core
            s_axis_mac_rx_tready = m_axis_core_rx_tready;

            if (s_axis_mac_rx_tvalid) begin
                m_axis_core_rx_tdata  = s_axis_mac_rx_tdata;
                m_axis_core_rx_tkeep  = s_axis_mac_rx_tkeep;
                m_axis_core_rx_tvalid = s_axis_mac_rx_tvalid;
                m_axis_core_rx_tlast  = s_axis_mac_rx_tlast;
                m_axis_core_rx_tuser  = s_axis_mac_rx_tuser;
            end

        end else begin
            // Route all other traffic to host (DMA)
            s_axis_mac_rx_tready = m_axis_dma_rx_tready;

            if (s_axis_mac_rx_tvalid) begin
                m_axis_dma_rx_tdata  = s_axis_mac_rx_tdata;
                m_axis_dma_rx_tkeep  = s_axis_mac_rx_tkeep;
                m_axis_dma_rx_tvalid = s_axis_mac_rx_tvalid;
                m_axis_dma_rx_tlast  = s_axis_mac_rx_tlast;
                m_axis_dma_rx_tuser  = s_axis_mac_rx_tuser;
            end
        end
    end

endmodule

`default_nettype wire