// SPDX-License-Identifier: BSD-2-Clause-Views
/*
 * Copyright (c) 2019-2024 The Regents of the University of California
 */

// Language: Verilog 2001

`resetall
`timescale 1ns / 1ps
`default_nettype none

/*
 * Transmit scheduler (round-robin)
 */
module tx_scheduler_rr #
(
    // Scheduler configuration
    parameter LEN_WIDTH = 16,
    parameter REQ_DEST_WIDTH = 8,
    parameter REQ_TAG_WIDTH = 8,
    parameter OP_TABLE_SIZE = 16,
    parameter QUEUE_INDEX_WIDTH = 6,
    parameter PIPELINE = 2,
    parameter SCHED_CTRL_ENABLE = 0,
    parameter REQ_DEST_DEFAULT = 0,

    // AXI lite interface configuration
    parameter AXIL_BASE_ADDR = 0,
    parameter AXIL_DATA_WIDTH = 32,
    parameter AXIL_ADDR_WIDTH = QUEUE_INDEX_WIDTH+2,
    parameter AXIL_STRB_WIDTH = (AXIL_DATA_WIDTH/8),

    // Register interface configuration
    parameter REG_ADDR_WIDTH = $clog2(32),
    parameter REG_DATA_WIDTH = AXIL_DATA_WIDTH,
    parameter REG_STRB_WIDTH = (REG_DATA_WIDTH/8),
    parameter RB_BLOCK_TYPE = 32'h0000C040,
    parameter RB_BASE_ADDR = 0,
    parameter RB_NEXT_PTR = 0
)
(
    input  wire                          clk,
    input  wire                          rst,

    /*
     * Control register interface
     */
    input  wire [REG_ADDR_WIDTH-1:0]     ctrl_reg_wr_addr,
    input  wire [REG_DATA_WIDTH-1:0]     ctrl_reg_wr_data,
    input  wire [REG_STRB_WIDTH-1:0]     ctrl_reg_wr_strb,
    input  wire                          ctrl_reg_wr_en,
    output wire                          ctrl_reg_wr_wait,
    output wire                          ctrl_reg_wr_ack,
    input  wire [REG_ADDR_WIDTH-1:0]     ctrl_reg_rd_addr,
    input  wire                          ctrl_reg_rd_en,
    output wire [REG_DATA_WIDTH-1:0]     ctrl_reg_rd_data,
    output wire                          ctrl_reg_rd_wait,
    output wire                          ctrl_reg_rd_ack,

    /*
     * Transmit request output (queue index)
     */
    output wire [QUEUE_INDEX_WIDTH-1:0]  m_axis_tx_req_queue,
    output wire [REQ_DEST_WIDTH-1:0]     m_axis_tx_req_dest,
    output wire [REQ_TAG_WIDTH-1:0]      m_axis_tx_req_tag,
    output wire                          m_axis_tx_req_valid,
    input  wire                          m_axis_tx_req_ready,

    /*
     * Transmit request status input
     */
    input  wire                          s_axis_tx_status_dequeue_empty,
    input  wire                          s_axis_tx_status_dequeue_error,
    input  wire [REQ_TAG_WIDTH-1:0]      s_axis_tx_status_dequeue_tag,
    input  wire                          s_axis_tx_status_dequeue_valid,

    input  wire                          s_axis_tx_status_start_error,
    input  wire [LEN_WIDTH-1:0]          s_axis_tx_status_start_len,
    input  wire [REQ_TAG_WIDTH-1:0]      s_axis_tx_status_start_tag,
    input  wire                          s_axis_tx_status_start_valid,

    input  wire [LEN_WIDTH-1:0]          s_axis_tx_status_finish_len,
    input  wire [REQ_TAG_WIDTH-1:0]      s_axis_tx_status_finish_tag,
    input  wire                          s_axis_tx_status_finish_valid,

    /*
     * Doorbell input
     */
    input  wire [QUEUE_INDEX_WIDTH-1:0]  s_axis_doorbell_queue,
    input  wire                          s_axis_doorbell_valid,

    /*
     * Scheduler control input
     */
    input  wire [QUEUE_INDEX_WIDTH-1:0]  s_axis_sched_ctrl_queue,
    input  wire                          s_axis_sched_ctrl_enable,
    input  wire                          s_axis_sched_ctrl_valid,
    output wire                          s_axis_sched_ctrl_ready,

    /*
     * AXI-Lite slave interface
     */
    input  wire [AXIL_ADDR_WIDTH-1:0]    s_axil_awaddr,
    input  wire [2:0]                    s_axil_awprot,
    input  wire                          s_axil_awvalid,
    output wire                          s_axil_awready,
    input  wire [AXIL_DATA_WIDTH-1:0]    s_axil_wdata,
    input  wire [AXIL_STRB_WIDTH-1:0]    s_axil_wstrb,
    input  wire                          s_axil_wvalid,
    output wire                          s_axil_wready,
    output wire [1:0]                    s_axil_bresp,
    output wire                          s_axil_bvalid,
    input  wire                          s_axil_bready,
    input  wire [AXIL_ADDR_WIDTH-1:0]    s_axil_araddr,
    input  wire [2:0]                    s_axil_arprot,
    input  wire                          s_axil_arvalid,
    output wire                          s_axil_arready,
    output wire [AXIL_DATA_WIDTH-1:0]    s_axil_rdata,
    output wire [1:0]                    s_axil_rresp,
    output wire                          s_axil_rvalid,
    input  wire                          s_axil_rready,

    /*
     * Control
     */
    input  wire                          enable,
    output wire                          active
);

localparam QUEUE_COUNT = 2**QUEUE_INDEX_WIDTH;

localparam CL_OP_TABLE_SIZE = $clog2(OP_TABLE_SIZE);

localparam RAM_BE_W = 2;
localparam RAM_WIDTH = RAM_BE_W*8;

localparam RBB = RB_BASE_ADDR & {REG_ADDR_WIDTH{1'b1}};

// check configuration
initial begin
    if (REQ_TAG_WIDTH < CL_OP_TABLE_SIZE) begin
        $error("Error: REQ_TAG_WIDTH insufficient for OP_TABLE_SIZE (instance %m)");
        $finish;
    end

    if (AXIL_DATA_WIDTH != 32) begin
        $error("Error: AXI lite interface width must be 32 (instance %m)");
        $finish;
    end

    if (AXIL_STRB_WIDTH * 8 != AXIL_DATA_WIDTH) begin
        $error("Error: AXI lite interface requires byte (8-bit) granularity (instance %m)");
        $finish;
    end

    if (AXIL_ADDR_WIDTH < QUEUE_INDEX_WIDTH+2) begin
        $error("Error: AXI lite address width too narrow (instance %m)");
        $finish;
    end

    if (PIPELINE < 2) begin
        $error("Error: PIPELINE must be at least 2 (instance %m)");
        $finish;
    end

    if (REG_DATA_WIDTH != 32) begin
        $error("Error: Register interface width must be 32 (instance %m)");
        $finish;
    end

    if (REG_STRB_WIDTH * 8 != REG_DATA_WIDTH) begin
        $error("Error: Register interface requires byte (8-bit) granularity (instance %m)");
        $finish;
    end

    if (REG_ADDR_WIDTH < $clog2(32)) begin
        $error("Error: Register address width too narrow (instance %m)");
        $finish;
    end

    if (RB_NEXT_PTR && RB_NEXT_PTR >= RB_BASE_ADDR && RB_NEXT_PTR < RB_BASE_ADDR + 32) begin
        $error("Error: RB_NEXT_PTR overlaps block (instance %m)");
        $finish;
    end
end

reg [PIPELINE-1:0] op_axil_write_pipe_reg = {PIPELINE{1'b0}}, op_axil_write_pipe_next;
reg [PIPELINE-1:0] op_axil_read_pipe_reg = {PIPELINE{1'b0}}, op_axil_read_pipe_next;
reg [PIPELINE-1:0] op_doorbell_pipe_reg = {PIPELINE{1'b0}}, op_doorbell_pipe_next;
reg [PIPELINE-1:0] op_req_pipe_reg = {PIPELINE{1'b0}}, op_req_pipe_next;
reg [PIPELINE-1:0] op_complete_pipe_reg = {PIPELINE{1'b0}}, op_complete_pipe_next;
reg [PIPELINE-1:0] op_ctrl_pipe_reg = {PIPELINE{1'b0}}, op_ctrl_pipe_next;
reg [PIPELINE-1:0] op_internal_pipe_reg = {PIPELINE{1'b0}}, op_internal_pipe_next;

reg [QUEUE_INDEX_WIDTH-1:0] queue_ram_addr_pipeline_reg[PIPELINE-1:0], queue_ram_addr_pipeline_next[PIPELINE-1:0];
reg [AXIL_DATA_WIDTH-1:0] write_data_pipeline_reg[PIPELINE-1:0], write_data_pipeline_next[PIPELINE-1:0];
reg [AXIL_STRB_WIDTH-1:0] write_strobe_pipeline_reg[PIPELINE-1:0], write_strobe_pipeline_next[PIPELINE-1:0];
reg [REQ_TAG_WIDTH-1:0] req_tag_pipeline_reg[PIPELINE-1:0], req_tag_pipeline_next[PIPELINE-1:0];
reg [CL_OP_TABLE_SIZE-1:0] op_index_pipeline_reg[PIPELINE-1:0], op_index_pipeline_next[PIPELINE-1:0];

reg [QUEUE_INDEX_WIDTH-1:0] m_axis_tx_req_queue_reg = {QUEUE_INDEX_WIDTH{1'b0}}, m_axis_tx_req_queue_next;
reg [REQ_DEST_WIDTH-1:0] m_axis_tx_req_dest_reg = REQ_DEST_DEFAULT;
reg [REQ_TAG_WIDTH-1:0] m_axis_tx_req_tag_reg = {REQ_TAG_WIDTH{1'b0}}, m_axis_tx_req_tag_next;
reg m_axis_tx_req_valid_reg = 1'b0, m_axis_tx_req_valid_next;

reg s_axis_sched_ctrl_ready_reg = 1'b0, s_axis_sched_ctrl_ready_next;

reg s_axil_awready_reg = 0, s_axil_awready_next;
reg s_axil_wready_reg = 0, s_axil_wready_next;
reg s_axil_bvalid_reg = 0, s_axil_bvalid_next;
reg s_axil_arready_reg = 0, s_axil_arready_next;
reg [AXIL_DATA_WIDTH-1:0] s_axil_rdata_reg = 0, s_axil_rdata_next;
reg s_axil_rvalid_reg = 0, s_axil_rvalid_next;

(* ramstyle = "no_rw_check" *)
reg [RAM_WIDTH-1:0] queue_ram[QUEUE_COUNT-1:0];
reg [QUEUE_INDEX_WIDTH-1:0] queue_ram_rd_addr;
reg [QUEUE_INDEX_WIDTH-1:0] queue_ram_wr_addr;
reg [RAM_WIDTH-1:0] queue_ram_wr_data;
reg queue_ram_wr_en;
reg [RAM_BE_W-1:0] queue_ram_wr_strb;
reg [RAM_WIDTH-1:0] queue_ram_rd_data_reg = 0;
reg [RAM_WIDTH-1:0] queue_ram_rd_data_pipe_reg[PIPELINE-1:1];

reg [RAM_WIDTH-1:0] queue_ram_rd_data_ovrd_pipe_reg[PIPELINE-1:0], queue_ram_rd_data_ovrd_pipe_next[PIPELINE-1:0];
reg [RAM_BE_W-1:0] queue_ram_rd_data_ovrd_en_pipe_reg[PIPELINE-1:0], queue_ram_rd_data_ovrd_en_pipe_next[PIPELINE-1:0];

reg [RAM_WIDTH-1:0] queue_ram_rd_data;

// Scheduler RAM entry:
// bit            len  field
// 0              1    enable
// 1              1    global_enable
// 2              1    sched_enable
// 6              1    active
// 7              1    scheduled
// 15:8           8   tail index

wire queue_ram_rd_data_enabled = queue_ram_rd_data[0];
wire queue_ram_rd_data_global_enable = queue_ram_rd_data[1];
wire queue_ram_rd_data_sched_enable = queue_ram_rd_data[2];
wire queue_ram_rd_data_active = queue_ram_rd_data[6];
wire queue_ram_rd_data_scheduled = queue_ram_rd_data[7];
wire [CL_OP_TABLE_SIZE-1:0] queue_ram_rd_data_op_tail_index = queue_ram_rd_data[15:8];

integer l;

always @* begin
    // apply read data override
    for (l = 0; l < RAM_BE_W; l = l + 1) begin
        // queue_ram_rd_data = queue_ram_rd_data_pipe_reg[PIPELINE-1];
        if (queue_ram_rd_data_ovrd_en_pipe_reg[PIPELINE-1][l]) begin
            queue_ram_rd_data[l*8 +: 8] = queue_ram_rd_data_ovrd_pipe_reg[PIPELINE-1][l*8 +: 8];
        end else begin
            queue_ram_rd_data[l*8 +: 8] = queue_ram_rd_data_pipe_reg[PIPELINE-1][l*8 +: 8];
        end
    end
end

reg [OP_TABLE_SIZE-1:0] op_table_active = 0;
(* ram_style = "distributed", ramstyle = "no_rw_check, mlab" *)
reg [QUEUE_INDEX_WIDTH-1:0] op_table_queue[OP_TABLE_SIZE-1:0];
(* ram_style = "distributed", ramstyle = "no_rw_check, mlab" *)
reg op_table_doorbell[OP_TABLE_SIZE-1:0];
(* ram_style = "distributed", ramstyle = "no_rw_check, mlab" *)
reg op_table_is_head[OP_TABLE_SIZE-1:0];
(* ram_style = "distributed", ramstyle = "no_rw_check, mlab" *)
reg [CL_OP_TABLE_SIZE-1:0] op_table_next_index[OP_TABLE_SIZE-1:0];
(* ram_style = "distributed", ramstyle = "no_rw_check, mlab" *)
reg [CL_OP_TABLE_SIZE-1:0] op_table_prev_index[OP_TABLE_SIZE-1:0];
wire [CL_OP_TABLE_SIZE-1:0] op_table_start_ptr;
wire op_table_start_ptr_valid;
reg [QUEUE_INDEX_WIDTH-1:0] op_table_start_queue;
reg op_table_start_en;
reg [CL_OP_TABLE_SIZE-1:0] op_table_doorbell_ptr;
reg op_table_doorbell_en;
reg [CL_OP_TABLE_SIZE-1:0] op_table_release_ptr;
reg op_table_release_en;
reg [CL_OP_TABLE_SIZE-1:0] op_table_update_next_ptr;
reg [CL_OP_TABLE_SIZE-1:0] op_table_update_next_index;
reg op_table_update_next_en;
reg [CL_OP_TABLE_SIZE-1:0] op_table_update_prev_ptr;
reg [CL_OP_TABLE_SIZE-1:0] op_table_update_prev_index;
reg op_table_update_prev_is_head;
reg op_table_update_prev_en;

reg [CL_OP_TABLE_SIZE+1-1:0] finish_fifo_wr_ptr_reg = 0, finish_fifo_wr_ptr_next;
reg [CL_OP_TABLE_SIZE+1-1:0] finish_fifo_rd_ptr_reg = 0, finish_fifo_rd_ptr_next;
(* ram_style = "distributed", ramstyle = "no_rw_check, mlab" *)
reg [REQ_TAG_WIDTH-1:0] finish_fifo_tag[(2**CL_OP_TABLE_SIZE)-1:0];
(* ram_style = "distributed", ramstyle = "no_rw_check, mlab" *)
reg finish_fifo_status[(2**CL_OP_TABLE_SIZE)-1:0];
reg finish_fifo_we;
reg [REQ_TAG_WIDTH-1:0] finish_fifo_wr_tag;
reg finish_fifo_wr_status;

reg [CL_OP_TABLE_SIZE-1:0] finish_ptr_reg = {CL_OP_TABLE_SIZE{1'b0}}, finish_ptr_next;
reg finish_status_reg = 1'b0, finish_status_next;
reg finish_valid_reg = 1'b0, finish_valid_next;

reg init_reg = 1'b0, init_next;
reg [QUEUE_INDEX_WIDTH-1:0] init_index_reg = 0, init_index_next;

reg [QUEUE_INDEX_WIDTH:0] active_queue_count_reg = 0, active_queue_count_next;

assign m_axis_tx_req_queue = m_axis_tx_req_queue_reg;
assign m_axis_tx_req_dest = m_axis_tx_req_dest_reg;
assign m_axis_tx_req_tag = m_axis_tx_req_tag_reg;
assign m_axis_tx_req_valid = m_axis_tx_req_valid_reg;

assign s_axis_sched_ctrl_ready = s_axis_sched_ctrl_ready_reg;

assign s_axil_awready = s_axil_awready_reg;
assign s_axil_wready = s_axil_wready_reg;
assign s_axil_bresp = 2'b00;
assign s_axil_bvalid = s_axil_bvalid_reg;
assign s_axil_arready = s_axil_arready_reg;
assign s_axil_rdata = s_axil_rdata_reg;
assign s_axil_rresp = 2'b00;
assign s_axil_rvalid = s_axil_rvalid_reg;

assign active = active_queue_count_reg != 0;

wire [QUEUE_INDEX_WIDTH-1:0] s_axil_awaddr_queue = s_axil_awaddr >> 2;
wire [QUEUE_INDEX_WIDTH-1:0] s_axil_araddr_queue = s_axil_araddr >> 2;

wire queue_tail_active = op_table_active[queue_ram_rd_data_op_tail_index] && op_table_queue[queue_ram_rd_data_op_tail_index] == queue_ram_addr_pipeline_reg[PIPELINE-1];

wire [QUEUE_INDEX_WIDTH-1:0] axis_doorbell_fifo_queue;
wire axis_doorbell_fifo_valid;
reg axis_doorbell_fifo_ready;

axis_fifo #(
    .DEPTH(256),
    .DATA_WIDTH(QUEUE_INDEX_WIDTH),
    .KEEP_ENABLE(0),
    .LAST_ENABLE(0),
    .ID_ENABLE(0),
    .DEST_ENABLE(0),
    .USER_ENABLE(0),
    .RAM_PIPELINE(1),
    .OUTPUT_FIFO_ENABLE(0),
    .FRAME_FIFO(0),
    .PAUSE_ENABLE(0)
)
doorbell_fifo (
    .clk(clk),
    .rst(rst),

    // AXI input
    .s_axis_tdata(s_axis_doorbell_queue),
    .s_axis_tkeep(0),
    .s_axis_tvalid(s_axis_doorbell_valid),
    .s_axis_tready(),
    .s_axis_tlast(0),
    .s_axis_tid(0),
    .s_axis_tdest(0),
    .s_axis_tuser(0),

    // AXI output
    .m_axis_tdata(axis_doorbell_fifo_queue),
    .m_axis_tkeep(),
    .m_axis_tvalid(axis_doorbell_fifo_valid),
    .m_axis_tready(axis_doorbell_fifo_ready),
    .m_axis_tlast(),
    .m_axis_tid(),
    .m_axis_tdest(),
    .m_axis_tuser(),

    // Pause
    .pause_req(),
    .pause_ack(),

    // Status
    .status_depth(),
    .status_depth_commit(),
    .status_overflow(),
    .status_bad_frame(),
    .status_good_frame()
);

reg [QUEUE_INDEX_WIDTH-1:0] axis_scheduler_fifo_in_queue;
reg axis_scheduler_fifo_in_valid;
wire axis_scheduler_fifo_in_ready;

wire [QUEUE_INDEX_WIDTH-1:0] axis_scheduler_fifo_out_queue;
wire axis_scheduler_fifo_out_valid;
reg axis_scheduler_fifo_out_ready;

axis_fifo #(
    .DEPTH(2**QUEUE_INDEX_WIDTH),
    .DATA_WIDTH(QUEUE_INDEX_WIDTH),
    .KEEP_ENABLE(0),
    .LAST_ENABLE(0),
    .ID_ENABLE(0),
    .DEST_ENABLE(0),
    .USER_ENABLE(0),
    .RAM_PIPELINE(1),
    .OUTPUT_FIFO_ENABLE(0),
    .FRAME_FIFO(0),
    .PAUSE_ENABLE(0)
)
rr_fifo (
    .clk(clk),
    .rst(rst),

    // AXI input
    .s_axis_tdata(axis_scheduler_fifo_in_queue),
    .s_axis_tkeep(0),
    .s_axis_tvalid(axis_scheduler_fifo_in_valid),
    .s_axis_tready(axis_scheduler_fifo_in_ready),
    .s_axis_tlast(0),
    .s_axis_tid(0),
    .s_axis_tdest(0),
    .s_axis_tuser(0),

    // AXI output
    .m_axis_tdata(axis_scheduler_fifo_out_queue),
    .m_axis_tkeep(),
    .m_axis_tvalid(axis_scheduler_fifo_out_valid),
    .m_axis_tready(axis_scheduler_fifo_out_ready),
    .m_axis_tlast(),
    .m_axis_tid(),
    .m_axis_tdest(),
    .m_axis_tuser(),

    // Pause
    .pause_req(),
    .pause_ack(),

    // Status
    .status_depth(),
    .status_depth_commit(),
    .status_overflow(),
    .status_bad_frame(),
    .status_good_frame()
);

priority_encoder #(
    .WIDTH(OP_TABLE_SIZE),
    .LSB_HIGH_PRIORITY(1)
)
op_table_start_enc_inst (
    .input_unencoded(~op_table_active),
    .output_valid(op_table_start_ptr_valid),
    .output_encoded(op_table_start_ptr),
    .output_unencoded()
);

integer i, j;

initial begin
    // break up loop to work around iteration termination
    for (i = 0; i < 2**QUEUE_INDEX_WIDTH; i = i + 2**(QUEUE_INDEX_WIDTH/2)) begin
        for (j = i; j < i + 2**(QUEUE_INDEX_WIDTH/2); j = j + 1) begin
            queue_ram[j] = 0;
        end
    end

    for (i = 0; i < PIPELINE; i = i + 1) begin
        queue_ram_addr_pipeline_reg[i] = 0;
        write_data_pipeline_reg[i] = 0;
        write_strobe_pipeline_reg[i] = 0;
        req_tag_pipeline_reg[i] = 0;

        queue_ram_rd_data_ovrd_pipe_reg[i] = 0;
        queue_ram_rd_data_ovrd_en_pipe_reg[i] = 0;
    end

    for (i = 0; i < OP_TABLE_SIZE; i = i + 1) begin
        op_table_queue[i] = 0;
        op_table_next_index[i] = 0;
        op_table_prev_index[i] = 0;
        op_table_doorbell[i] = 0;
        op_table_is_head[i] = 0;
    end
end

// control registers
reg ctrl_reg_wr_ack_reg = 1'b0;
reg [REG_DATA_WIDTH-1:0] ctrl_reg_rd_data_reg = {REG_DATA_WIDTH{1'b0}};
reg ctrl_reg_rd_ack_reg = 1'b0;

reg enable_reg = 1'b0;

assign ctrl_reg_wr_wait = 1'b0;
assign ctrl_reg_wr_ack = ctrl_reg_wr_ack_reg;
assign ctrl_reg_rd_data = ctrl_reg_rd_data_reg;
assign ctrl_reg_rd_wait = 1'b0;
assign ctrl_reg_rd_ack = ctrl_reg_rd_ack_reg;

integer k;

always @(posedge clk) begin
    ctrl_reg_wr_ack_reg <= 1'b0;
    ctrl_reg_rd_data_reg <= {REG_DATA_WIDTH{1'b0}};
    ctrl_reg_rd_ack_reg <= 1'b0;

    if (ctrl_reg_wr_en && !ctrl_reg_wr_ack_reg) begin
        // write operation
        ctrl_reg_wr_ack_reg <= 1'b1;
        case ({ctrl_reg_wr_addr >> 2, 2'b00})
            // Round-robin scheduler
            RBB+8'h18: begin
                // Sched: control
                enable_reg <= ctrl_reg_wr_data[0];
            end
            RBB+8'h1C: m_axis_tx_req_dest_reg <= ctrl_reg_wr_data;  // Sched: dest
            default: ctrl_reg_wr_ack_reg <= 1'b0;
        endcase
    end

    if (ctrl_reg_rd_en && !ctrl_reg_rd_ack_reg) begin
        // read operation
        ctrl_reg_rd_ack_reg <= 1'b1;
        case ({ctrl_reg_rd_addr >> 2, 2'b00})
            // Round-robin scheduler
            RBB+8'h00: ctrl_reg_rd_data_reg <= RB_BLOCK_TYPE;         // Sched: Type
            RBB+8'h04: ctrl_reg_rd_data_reg <= 32'h00000100;          // Sched: Version
            RBB+8'h08: ctrl_reg_rd_data_reg <= RB_NEXT_PTR;           // Sched: Next header
            RBB+8'h0C: ctrl_reg_rd_data_reg <= AXIL_BASE_ADDR;        // Sched: Offset
            RBB+8'h10: ctrl_reg_rd_data_reg <= 2**QUEUE_INDEX_WIDTH;  // Sched: Channel count
            RBB+8'h14: ctrl_reg_rd_data_reg <= 4;                     // Sched: Channel stride
            RBB+8'h18: begin
                // Sched: control
                ctrl_reg_rd_data_reg[0] <= enable_reg;
                ctrl_reg_rd_data_reg[8] <= active_queue_count_reg != 0;
            end
            RBB+8'h1C: ctrl_reg_rd_data_reg <= m_axis_tx_req_dest_reg;  // Sched: dest
            default: ctrl_reg_rd_ack_reg <= 1'b0;
        endcase
    end

    if (rst) begin
        ctrl_reg_wr_ack_reg <= 1'b0;
        ctrl_reg_rd_ack_reg <= 1'b0;

        enable_reg <= 1'b0;
        m_axis_tx_req_dest_reg <= REQ_DEST_DEFAULT;
    end
end

always @* begin
    op_axil_write_pipe_next = {op_axil_write_pipe_reg, 1'b0};
    op_axil_read_pipe_next = {op_axil_read_pipe_reg, 1'b0};
    op_doorbell_pipe_next = {op_doorbell_pipe_reg, 1'b0};
    op_req_pipe_next = {op_req_pipe_reg, 1'b0};
    op_complete_pipe_next = {op_complete_pipe_reg, 1'b0};
    op_ctrl_pipe_next = {op_ctrl_pipe_reg, 1'b0};
    op_internal_pipe_next = {op_internal_pipe_reg, 1'b0};

    queue_ram_addr_pipeline_next[0] = 0;
    write_data_pipeline_next[0] = 0;
    write_strobe_pipeline_next[0] = 0;
    req_tag_pipeline_next[0] = 0;
    op_index_pipeline_next[0] = 0;

    queue_ram_rd_data_ovrd_pipe_next[0] = 0;
    queue_ram_rd_data_ovrd_en_pipe_next[0] = 0;

    for (j = 1; j < PIPELINE; j = j + 1) begin
        queue_ram_addr_pipeline_next[j] = queue_ram_addr_pipeline_reg[j-1];
        write_data_pipeline_next[j] = write_data_pipeline_reg[j-1];
        write_strobe_pipeline_next[j] = write_strobe_pipeline_reg[j-1];
        req_tag_pipeline_next[j] = req_tag_pipeline_reg[j-1];
        op_index_pipeline_next[j] = op_index_pipeline_reg[j-1];

        queue_ram_rd_data_ovrd_pipe_next[j] = queue_ram_rd_data_ovrd_pipe_reg[j-1];
        queue_ram_rd_data_ovrd_en_pipe_next[j] = queue_ram_rd_data_ovrd_en_pipe_reg[j-1];
    end

    m_axis_tx_req_queue_next = m_axis_tx_req_queue_reg;
    m_axis_tx_req_tag_next = m_axis_tx_req_tag_reg;
    m_axis_tx_req_valid_next = m_axis_tx_req_valid_reg && !m_axis_tx_req_ready;

    s_axis_sched_ctrl_ready_next = 1'b0;

    s_axil_awready_next = 1'b0;
    s_axil_wready_next = 1'b0;
    s_axil_bvalid_next = s_axil_bvalid_reg && !s_axil_bready;

    s_axil_arready_next = 1'b0;
    s_axil_rdata_next = s_axil_rdata_reg;
    s_axil_rvalid_next = s_axil_rvalid_reg && !s_axil_rready;

    queue_ram_rd_addr = 0;
    queue_ram_wr_addr = queue_ram_addr_pipeline_reg[PIPELINE-1];
    queue_ram_wr_data = queue_ram_rd_data;
    queue_ram_wr_en = 0;
    queue_ram_wr_strb = 0;

    op_table_start_queue = queue_ram_addr_pipeline_reg[PIPELINE-1];
    op_table_start_en = 1'b0;
    op_table_doorbell_ptr = queue_ram_rd_data_op_tail_index;
    op_table_doorbell_en = 1'b0;
    op_table_release_ptr = op_index_pipeline_reg[PIPELINE-1];
    op_table_release_en = 1'b0;
    op_table_update_next_ptr = queue_ram_rd_data_op_tail_index;
    op_table_update_next_index = op_index_pipeline_reg[PIPELINE-1];
    op_table_update_next_en = 1'b0;
    op_table_update_prev_ptr = op_index_pipeline_reg[PIPELINE-1];
    op_table_update_prev_index = queue_ram_rd_data_op_tail_index;
    op_table_update_prev_is_head = !(queue_tail_active && op_index_pipeline_reg[PIPELINE-1] != queue_ram_rd_data_op_tail_index);
    op_table_update_prev_en = 1'b0;

    finish_fifo_rd_ptr_next = finish_fifo_rd_ptr_reg;
    finish_fifo_wr_ptr_next = finish_fifo_wr_ptr_reg;
    finish_fifo_we = 1'b0;
    finish_fifo_wr_tag = s_axis_tx_status_dequeue_tag;
    finish_fifo_wr_status = !s_axis_tx_status_dequeue_error && !s_axis_tx_status_dequeue_empty;

    finish_ptr_next = finish_ptr_reg;
    finish_status_next = finish_status_reg;
    finish_valid_next = finish_valid_reg;

    init_next = init_reg;
    init_index_next = init_index_reg;

    active_queue_count_next = active_queue_count_reg;

    axis_doorbell_fifo_ready = 1'b0;

    axis_scheduler_fifo_in_queue = queue_ram_addr_pipeline_reg[PIPELINE-1];
    axis_scheduler_fifo_in_valid = 1'b0;

    axis_scheduler_fifo_out_ready = 1'b0;

    // pipeline stage 0 - receive request
    if (!init_reg) begin
        // init queue states
        op_internal_pipe_next[0] = 1'b1;

        init_index_next = init_index_reg + 1;

        queue_ram_rd_addr = init_index_reg;
        queue_ram_addr_pipeline_next[0] = init_index_reg;

        if (init_index_reg == {QUEUE_INDEX_WIDTH{1'b1}}) begin
            init_next = 1'b1;
        end
    end else if (s_axil_awvalid && s_axil_wvalid && (!s_axil_bvalid || s_axil_bready) && !op_axil_write_pipe_reg) begin
        // AXIL write
        op_axil_write_pipe_next[0] = 1'b1;

        s_axil_awready_next = 1'b1;
        s_axil_wready_next = 1'b1;

        write_data_pipeline_next[0] = s_axil_wdata;
        write_strobe_pipeline_next[0] = s_axil_wstrb;

        queue_ram_rd_addr = s_axil_awaddr_queue;
        queue_ram_addr_pipeline_next[0] = s_axil_awaddr_queue;
    end else if (s_axil_arvalid && (!s_axil_rvalid || s_axil_rready) && !op_axil_read_pipe_reg) begin
        // AXIL read
        op_axil_read_pipe_next[0] = 1'b1;

        s_axil_arready_next = 1'b1;

        queue_ram_rd_addr = s_axil_araddr_queue;
        queue_ram_addr_pipeline_next[0] = s_axil_araddr_queue;
    end else if (axis_doorbell_fifo_valid) begin
        // handle doorbell
        op_doorbell_pipe_next[0] = 1'b1;

        axis_doorbell_fifo_ready = 1'b1;

        queue_ram_rd_addr = axis_doorbell_fifo_queue;
        queue_ram_addr_pipeline_next[0] = axis_doorbell_fifo_queue;
    end else if (finish_valid_reg && !op_complete_pipe_reg[0]) begin
        // transmit complete
        op_complete_pipe_next[0] = 1'b1;

        write_data_pipeline_next[0][0] = finish_status_reg || op_table_doorbell[finish_ptr_reg];
        op_index_pipeline_next[0] = finish_ptr_reg;

        finish_valid_next = 1'b0;

        queue_ram_rd_addr = op_table_queue[finish_ptr_reg];
        queue_ram_addr_pipeline_next[0] = op_table_queue[finish_ptr_reg];
    end else if (SCHED_CTRL_ENABLE && s_axis_sched_ctrl_valid && !op_ctrl_pipe_reg[0]) begin
        // Scheduler control
        op_ctrl_pipe_next[0] = 1'b1;

        s_axis_sched_ctrl_ready_next = 1'b1;

        write_data_pipeline_next[0] = s_axis_sched_ctrl_enable;

        queue_ram_rd_addr = s_axis_sched_ctrl_queue;
        queue_ram_addr_pipeline_next[0] = s_axis_sched_ctrl_queue;
    end else if (enable && enable_reg && op_table_start_ptr_valid && axis_scheduler_fifo_out_valid && (!m_axis_tx_req_valid || m_axis_tx_req_ready) && !op_req_pipe_reg) begin
        // transmit request
        op_req_pipe_next[0] = 1'b1;

        op_table_start_en = 1'b1;
        op_table_start_queue = axis_scheduler_fifo_out_queue;

        op_index_pipeline_next[0] = op_table_start_ptr;

        axis_scheduler_fifo_out_ready = 1'b1;

        queue_ram_rd_addr = axis_scheduler_fifo_out_queue;
        queue_ram_addr_pipeline_next[0] = axis_scheduler_fifo_out_queue;
    end

    // read complete, perform operation
    if (op_internal_pipe_reg[PIPELINE-1]) begin
        // internal operation

        // init queue state
        queue_ram_wr_addr = queue_ram_addr_pipeline_reg[PIPELINE-1];
        queue_ram_wr_data[0] = 1'b0; // queue enabled
        if (SCHED_CTRL_ENABLE) begin
            queue_ram_wr_data[1] = 1'b0; // queue global enable
            queue_ram_wr_data[2] = 1'b0; // queue sched enable
        end
        queue_ram_wr_data[6] = 1'b0; // queue active
        queue_ram_wr_data[7] = 1'b0; // queue scheduled
        queue_ram_wr_strb[0] = 1'b1;
        queue_ram_wr_en = 1'b1;
    end else if (op_doorbell_pipe_reg[PIPELINE-1]) begin
        // handle doorbell

        // mark queue active
        queue_ram_wr_addr = queue_ram_addr_pipeline_reg[PIPELINE-1];
        queue_ram_wr_data[6] = 1'b1; // queue active
        queue_ram_wr_strb[0] = 1'b1;
        queue_ram_wr_en = 1'b1;

        // schedule queue if necessary
        if (queue_ram_rd_data_enabled && (!SCHED_CTRL_ENABLE || queue_ram_rd_data_global_enable || queue_ram_rd_data_sched_enable) && !queue_ram_rd_data_scheduled) begin
            queue_ram_wr_data[7] = 1'b1; // queue scheduled

            axis_scheduler_fifo_in_queue = queue_ram_addr_pipeline_reg[PIPELINE-1];
            axis_scheduler_fifo_in_valid = 1'b1;

            active_queue_count_next = active_queue_count_reg + 1;
        end

        if (queue_tail_active) begin
            // record doorbell in table so we don't lose it
            op_table_doorbell_ptr = queue_ram_rd_data_op_tail_index;
            op_table_doorbell_en = 1'b1;
        end
    end else if (op_req_pipe_reg[PIPELINE-1]) begin
        // transmit request
        m_axis_tx_req_queue_next = queue_ram_addr_pipeline_reg[PIPELINE-1];
        m_axis_tx_req_tag_next = op_index_pipeline_reg[PIPELINE-1];

        axis_scheduler_fifo_in_queue = queue_ram_addr_pipeline_reg[PIPELINE-1];

        // update state
        queue_ram_wr_addr = queue_ram_addr_pipeline_reg[PIPELINE-1];
        queue_ram_wr_data[15:8] = op_index_pipeline_reg[PIPELINE-1]; // tail index
        queue_ram_wr_strb[0] = 1'b1;
        queue_ram_wr_en = 1'b1;

        op_table_update_prev_ptr = op_index_pipeline_reg[PIPELINE-1];
        op_table_update_prev_index = queue_ram_rd_data_op_tail_index;
        op_table_update_prev_is_head = !(queue_tail_active && op_index_pipeline_reg[PIPELINE-1] != queue_ram_rd_data_op_tail_index);

        op_table_update_next_ptr = queue_ram_rd_data_op_tail_index;
        op_table_update_next_index = op_index_pipeline_reg[PIPELINE-1];

        if (queue_ram_rd_data_enabled && (!SCHED_CTRL_ENABLE || queue_ram_rd_data_global_enable || queue_ram_rd_data_sched_enable) && queue_ram_rd_data_active && queue_ram_rd_data_scheduled) begin
            // queue enabled, active, and scheduled

            // issue transmit request
            m_axis_tx_req_valid_next = 1'b1;

            // reschedule
            axis_scheduler_fifo_in_valid = 1'b1;

            // update state
            queue_ram_wr_data[7] = 1'b1; // queue scheduled
            queue_ram_wr_strb[1] = 1'b1; // tail index

            op_table_update_prev_en = 1'b1;
            op_table_update_next_en = queue_tail_active && op_index_pipeline_reg[PIPELINE-1] != queue_ram_rd_data_op_tail_index;
        end else begin
            // queue not enabled, not active, or not scheduled
            // deschedule queue

            op_table_release_ptr = op_index_pipeline_reg[PIPELINE-1];
            op_table_release_en = 1'b1;

            // update state
            queue_ram_wr_data[7] = 1'b0; // queue scheduled

            if (queue_ram_rd_data_scheduled) begin
                active_queue_count_next = active_queue_count_reg - 1;
            end
        end
    end else if (op_complete_pipe_reg[PIPELINE-1]) begin
        // tx complete

        // update state
        queue_ram_wr_addr = queue_ram_addr_pipeline_reg[PIPELINE-1];
        queue_ram_wr_strb[0] = 1'b1;
        queue_ram_wr_en = 1'b1;

        op_table_update_prev_ptr = op_table_next_index[op_index_pipeline_reg[PIPELINE-1]];
        op_table_update_prev_index = op_table_prev_index[op_index_pipeline_reg[PIPELINE-1]];
        op_table_update_prev_is_head = op_table_is_head[op_index_pipeline_reg[PIPELINE-1]];
        op_table_update_prev_en = op_index_pipeline_reg[PIPELINE-1] != queue_ram_rd_data_op_tail_index; // our next pointer only valid if we're not the tail

        op_table_update_next_ptr = op_table_prev_index[op_index_pipeline_reg[PIPELINE-1]];
        op_table_update_next_index = op_table_next_index[op_index_pipeline_reg[PIPELINE-1]];
        op_table_update_next_en = !op_table_is_head[op_index_pipeline_reg[PIPELINE-1]]; // our prev index only valid if we're not the head element

        op_table_doorbell_ptr = op_table_prev_index[op_index_pipeline_reg[PIPELINE-1]];
        op_table_doorbell_en = !op_table_is_head[op_index_pipeline_reg[PIPELINE-1]] && op_table_doorbell[op_index_pipeline_reg[PIPELINE-1]];;

        op_table_release_ptr = op_index_pipeline_reg[PIPELINE-1];
        op_table_release_en = 1'b1;

        if (write_data_pipeline_reg[PIPELINE-1][0]) begin
            queue_ram_wr_data[6] = 1'b1; // queue active

            // schedule if disabled
            if ((!SCHED_CTRL_ENABLE || write_data_pipeline_reg[PIPELINE-1][1] || queue_ram_rd_data_sched_enable) && !queue_ram_rd_data_scheduled) begin
                queue_ram_wr_data[7] = 1'b1; // queue scheduled

                axis_scheduler_fifo_in_queue = queue_ram_addr_pipeline_reg[PIPELINE-1];
                axis_scheduler_fifo_in_valid = 1'b1;

                active_queue_count_next = active_queue_count_reg + 1;
            end
        end else begin
            queue_ram_wr_data[6] = 1'b0; // queue active
        end
    end else if (SCHED_CTRL_ENABLE && op_ctrl_pipe_reg[PIPELINE-1]) begin
        // Scheduler control
        queue_ram_wr_addr = queue_ram_addr_pipeline_reg[PIPELINE-1];
        queue_ram_wr_en = 1'b1;

        queue_ram_wr_data[2] = write_data_pipeline_reg[PIPELINE-1][0]; // queue sched enable
        queue_ram_wr_strb[0] = 1'b1;

        // schedule if disabled
        if (queue_ram_rd_data_enabled && queue_ram_rd_data_active && (queue_ram_rd_data_global_enable || write_data_pipeline_reg[PIPELINE-1][0]) && !queue_ram_rd_data_scheduled) begin
            queue_ram_wr_data[7] = 1'b1; // queue scheduled

            axis_scheduler_fifo_in_queue = queue_ram_addr_pipeline_reg[PIPELINE-1];
            axis_scheduler_fifo_in_valid = 1'b1;

            active_queue_count_next = active_queue_count_reg + 1;
        end
    end else if (op_axil_write_pipe_reg[PIPELINE-1]) begin
        // AXIL write
        s_axil_bvalid_next = 1'b1;

        queue_ram_wr_addr = queue_ram_addr_pipeline_reg[PIPELINE-1];
        queue_ram_wr_en = 1'b1;

        queue_ram_wr_data[0] = write_data_pipeline_reg[PIPELINE-1][0]; // queue enabled
        queue_ram_wr_data[1] = write_data_pipeline_reg[PIPELINE-1][1]; // queue global enable
        queue_ram_wr_strb[0] = write_strobe_pipeline_reg[PIPELINE-1][0];

        // schedule if disabled
        if (write_data_pipeline_reg[PIPELINE-1][0] && queue_ram_rd_data_active && (!SCHED_CTRL_ENABLE || write_data_pipeline_reg[PIPELINE-1][1] || queue_ram_rd_data_sched_enable) && !queue_ram_rd_data_scheduled) begin
            queue_ram_wr_data[7] = 1'b1; // queue scheduled

            axis_scheduler_fifo_in_queue = queue_ram_addr_pipeline_reg[PIPELINE-1];
            axis_scheduler_fifo_in_valid = 1'b1;

            active_queue_count_next = active_queue_count_reg + 1;
        end
    end else if (op_axil_read_pipe_reg[PIPELINE-1]) begin
        // AXIL read
        s_axil_rvalid_next = 1'b1;
        s_axil_rdata_next = 0;

        s_axil_rdata_next[0] = queue_ram_rd_data_enabled;
        if (SCHED_CTRL_ENABLE) begin
            s_axil_rdata_next[1] = queue_ram_rd_data_global_enable;
            s_axil_rdata_next[2] = queue_ram_rd_data_sched_enable;
        end
        s_axil_rdata_next[16] = queue_ram_rd_data_active;
        s_axil_rdata_next[24] = queue_ram_rd_data_scheduled;
    end

    // handle read data override
    for (j = 0; j < RAM_BE_W; j = j + 1) begin
        if (queue_ram_wr_en && queue_ram_wr_strb[j]) begin
            for (k = 0; k < PIPELINE; k = k + 1) begin
                if (queue_ram_wr_addr == queue_ram_addr_pipeline_next[k]) begin
                    queue_ram_rd_data_ovrd_pipe_next[k][j*8 +: 8] = queue_ram_wr_data[j*8 +: 8];
                    queue_ram_rd_data_ovrd_en_pipe_next[k][j] = 1'b1;
                end
            end
        end
    end

    // finish transmit operation
    if (s_axis_tx_status_dequeue_valid) begin
        finish_fifo_we = 1'b1;
        finish_fifo_wr_tag = s_axis_tx_status_dequeue_tag;
        finish_fifo_wr_status = !s_axis_tx_status_dequeue_error && !s_axis_tx_status_dequeue_empty;
        finish_fifo_wr_ptr_next = finish_fifo_wr_ptr_reg + 1;
    end

    if (!finish_valid_reg && finish_fifo_wr_ptr_reg != finish_fifo_rd_ptr_reg) begin
        finish_ptr_next = finish_fifo_tag[finish_fifo_rd_ptr_reg[CL_OP_TABLE_SIZE-1:0]];
        finish_status_next = finish_fifo_status[finish_fifo_rd_ptr_reg[CL_OP_TABLE_SIZE-1:0]];
        finish_valid_next = 1'b1;
        finish_fifo_rd_ptr_next = finish_fifo_rd_ptr_reg + 1;
    end
end

always @(posedge clk) begin
    op_axil_write_pipe_reg <= op_axil_write_pipe_next;
    op_axil_read_pipe_reg <= op_axil_read_pipe_next;
    op_doorbell_pipe_reg <= op_doorbell_pipe_next;
    op_req_pipe_reg <= op_req_pipe_next;
    op_complete_pipe_reg <= op_complete_pipe_next;
    op_ctrl_pipe_reg <= op_ctrl_pipe_next;
    op_internal_pipe_reg <= op_internal_pipe_next;

    finish_fifo_rd_ptr_reg <= finish_fifo_rd_ptr_next;
    finish_fifo_wr_ptr_reg <= finish_fifo_wr_ptr_next;

    finish_ptr_reg <= finish_ptr_next;
    finish_status_reg <= finish_status_next;
    finish_valid_reg <= finish_valid_next;

    m_axis_tx_req_queue_reg <= m_axis_tx_req_queue_next;
    m_axis_tx_req_tag_reg <= m_axis_tx_req_tag_next;
    m_axis_tx_req_valid_reg <= m_axis_tx_req_valid_next;

    s_axis_sched_ctrl_ready_reg <= s_axis_sched_ctrl_ready_next;

    s_axil_awready_reg <= s_axil_awready_next;
    s_axil_wready_reg <= s_axil_wready_next;
    s_axil_bvalid_reg <= s_axil_bvalid_next;
    s_axil_arready_reg <= s_axil_arready_next;
    s_axil_rdata_reg <= s_axil_rdata_next;
    s_axil_rvalid_reg <= s_axil_rvalid_next;

    init_reg <= init_next;
    init_index_reg <= init_index_next;

    active_queue_count_reg <= active_queue_count_next;

    for (i = 0; i < PIPELINE; i = i + 1) begin
        queue_ram_addr_pipeline_reg[i] <= queue_ram_addr_pipeline_next[i];
        write_data_pipeline_reg[i] <= write_data_pipeline_next[i];
        write_strobe_pipeline_reg[i] <= write_strobe_pipeline_next[i];
        req_tag_pipeline_reg[i] <= req_tag_pipeline_next[i];
        op_index_pipeline_reg[i] <= op_index_pipeline_next[i];

        queue_ram_rd_data_ovrd_pipe_reg[i] <= queue_ram_rd_data_ovrd_pipe_next[i];
        queue_ram_rd_data_ovrd_en_pipe_reg[i] <= queue_ram_rd_data_ovrd_en_pipe_next[i];
    end

    if (queue_ram_wr_en) begin
        for (i = 0; i < RAM_BE_W; i = i + 1) begin
            if (queue_ram_wr_strb[i]) begin
                queue_ram[queue_ram_wr_addr][i*8 +: 8] <= queue_ram_wr_data[i*8 +: 8];
            end
        end
    end
    queue_ram_rd_data_reg <= queue_ram[queue_ram_rd_addr];
    queue_ram_rd_data_pipe_reg[1] <= queue_ram_rd_data_reg;
    for (i = 2; i < PIPELINE; i = i + 1) begin
        queue_ram_rd_data_pipe_reg[i] <= queue_ram_rd_data_pipe_reg[i-1];
    end

    if (op_table_start_en) begin
        op_table_queue[op_table_start_ptr] <= op_table_start_queue;
        op_table_doorbell[op_table_start_ptr] <= 1'b0;
        op_table_active[op_table_start_ptr] <= 1'b1;
    end
    if (op_table_doorbell_en) begin
        op_table_doorbell[op_table_doorbell_ptr] <= 1'b1;
    end
    if (op_table_update_next_en) begin
        op_table_next_index[op_table_update_next_ptr] <= op_table_update_next_index;
    end
    if (op_table_update_prev_en) begin
        op_table_prev_index[op_table_update_prev_ptr] <= op_table_update_prev_index;
        op_table_is_head[op_table_update_prev_ptr] <= op_table_update_prev_is_head;
    end
    if (op_table_release_en) begin
        op_table_active[op_table_release_ptr] <= 1'b0;
    end

    if (finish_fifo_we) begin
        finish_fifo_tag[finish_fifo_wr_ptr_reg[CL_OP_TABLE_SIZE-1:0]] <= finish_fifo_wr_tag;
        finish_fifo_status[finish_fifo_wr_ptr_reg[CL_OP_TABLE_SIZE-1:0]] <= finish_fifo_wr_status;
    end

    if (rst) begin
        op_axil_write_pipe_reg <= {PIPELINE{1'b0}};
        op_axil_read_pipe_reg <= {PIPELINE{1'b0}};
        op_doorbell_pipe_reg <= {PIPELINE{1'b0}};
        op_req_pipe_reg <= {PIPELINE{1'b0}};
        op_complete_pipe_reg <= {PIPELINE{1'b0}};
        op_ctrl_pipe_reg <= {PIPELINE{1'b0}};
        op_internal_pipe_reg <= {PIPELINE{1'b0}};

        finish_fifo_rd_ptr_reg <= {CL_OP_TABLE_SIZE+1{1'b0}};
        finish_fifo_wr_ptr_reg <= {CL_OP_TABLE_SIZE+1{1'b0}};

        finish_valid_reg <= 1'b0;

        m_axis_tx_req_valid_reg <= 1'b0;

        s_axis_sched_ctrl_ready_reg <= 1'b0;

        s_axil_awready_reg <= 1'b0;
        s_axil_wready_reg <= 1'b0;
        s_axil_bvalid_reg <= 1'b0;
        s_axil_arready_reg <= 1'b0;
        s_axil_rvalid_reg <= 1'b0;

        init_reg <= 1'b0;
        init_index_reg <= 0;

        active_queue_count_reg <= 0;

        op_table_active <= 0;
    end
end

endmodule

`resetall
