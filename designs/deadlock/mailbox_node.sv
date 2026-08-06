`timescale 1ns / 1ps
//
// One node of a two-node mailbox pair: an AXI4-Lite master port that writes to
// the peer, and an AXI4-Lite slave port the peer writes to.
//
// The bug this design exists to carry (§8.18) is `HOLD`. With it set, the node
// will not accept an incoming write while one of its own is still outstanding —
// a single shared state machine serving both directions, which is one of the
// most common ways a real interconnect deadlocks. Two such nodes writing to each
// other at the same moment wait for each other forever:
//
//     node0 holds its own write open  ->  refuses node1's
//     node1 holds its own write open  ->  refuses node0's
//
// With `HOLD` clear the slave side is independent of the master side, the two
// writes cross, and the same testbench runs to completion. That is the whole
// point of making it a parameter: the detector has to tell the two runs apart,
// and "no deadlock found" has to be as checkable as "deadlock found".
//
module mailbox_node #(
    parameter int          N_TXN       = 4,
    // Cycles after reset before this node starts. Both nodes start together in
    // the deadlocking run, on purpose: a deadlock needs the requests to cross.
    parameter int          START_DELAY = 0,
    // Wait states on the slave side, so the trace has latency in it.
    parameter int          WAIT_STATES = 2,
    parameter logic [31:0] PEER_BASE   = 32'h0000_0000,
    // The injected bug. 1 = hold-and-wait, 0 = the same design, correct.
    parameter bit          HOLD        = 1'b1
) (
    input  logic        aclk,
    input  logic        aresetn,

    // ---- master port: this node writing to the peer ----------------------
    output logic        m_awvalid,
    input  logic        m_awready,
    output logic [31:0] m_awaddr,
    output logic [ 2:0] m_awprot,
    output logic        m_wvalid,
    input  logic        m_wready,
    output logic [31:0] m_wdata,
    output logic [ 3:0] m_wstrb,
    input  logic        m_bvalid,
    output logic        m_bready,
    input  logic [ 1:0] m_bresp,
    output logic        m_arvalid,
    input  logic        m_arready,
    output logic [31:0] m_araddr,
    output logic [ 2:0] m_arprot,
    input  logic        m_rvalid,
    output logic        m_rready,
    input  logic [31:0] m_rdata,
    input  logic [ 1:0] m_rresp,

    // ---- slave port: the peer writing to this node -----------------------
    input  logic        s_awvalid,
    output logic        s_awready,
    input  logic [31:0] s_awaddr,
    input  logic [ 2:0] s_awprot,
    input  logic        s_wvalid,
    output logic        s_wready,
    input  logic [31:0] s_wdata,
    input  logic [ 3:0] s_wstrb,
    output logic        s_bvalid,
    input  logic        s_bready,
    output logic [ 1:0] s_bresp,
    input  logic        s_arvalid,
    output logic        s_arready,
    input  logic [31:0] s_araddr,
    input  logic [ 2:0] s_arprot,
    output logic        s_rvalid,
    input  logic        s_rready,
    output logic [31:0] s_rdata,
    output logic [ 1:0] s_rresp,

    // High until this node has sent all of its writes and answered all it owes.
    output logic        busy
);

  localparam logic [1:0] OKAY = 2'b00;

  // ---- master side --------------------------------------------------------

  typedef enum logic [1:0] {M_WAIT, M_ADDR, M_RESP, M_DONE} m_state_e;

  m_state_e    mstate;
  logic [7:0]  sent;
  logic [15:0] delay;
  logic        aw_done, w_done;

  // valid is asserted on entering M_ADDR and never withdrawn before ready —
  // withdrawing it is the protocol error the packs' NODROP rule looks for, and
  // a design built to be analysed must not commit it by accident.
  assign m_awvalid = (mstate == M_ADDR) && !aw_done;
  assign m_wvalid  = (mstate == M_ADDR) && !w_done;
  assign m_bready  = (mstate == M_RESP);
  assign m_awaddr  = PEER_BASE + {24'h0, sent[1:0], 2'b00};
  assign m_wdata   = {24'h0, sent};
  assign m_wstrb   = 4'hF;
  assign m_awprot  = 3'b000;

  // The read channels exist so the interface is a complete AXI4-Lite port and
  // is recognised as one; this design has nothing to read.
  assign m_arvalid = 1'b0;
  assign m_araddr  = 32'h0;
  assign m_arprot  = 3'b000;
  assign m_rready  = 1'b0;

  // What makes the deadlock: an outgoing write that has not been answered yet.
  wire out_pending = (mstate == M_ADDR) || (mstate == M_RESP);

  wire aw_ok = aw_done || (m_awvalid && m_awready);
  wire w_ok  = w_done  || (m_wvalid  && m_wready);

  always_ff @(posedge aclk or negedge aresetn) begin
    if (!aresetn) begin
      mstate  <= M_WAIT;
      sent    <= '0;
      delay   <= '0;
      aw_done <= 1'b0;
      w_done  <= 1'b0;
    end else begin
      case (mstate)
        M_WAIT:
          if (delay >= START_DELAY[15:0]) mstate <= (sent >= N_TXN[7:0]) ? M_DONE : M_ADDR;
          else delay <= delay + 1'b1;
        M_ADDR: begin
          if (m_awvalid && m_awready) aw_done <= 1'b1;
          if (m_wvalid && m_wready) w_done <= 1'b1;
          if (aw_ok && w_ok) begin
            mstate  <= M_RESP;
            aw_done <= 1'b0;
            w_done  <= 1'b0;
          end
        end
        M_RESP:
          if (m_bvalid) begin
            sent   <= sent + 1'b1;
            mstate <= (sent + 8'd1 >= N_TXN[7:0]) ? M_DONE : M_ADDR;
          end
        M_DONE: mstate <= M_DONE;
        default: mstate <= M_WAIT;
      endcase
    end
  end

  // ---- slave side ---------------------------------------------------------

  logic [31:0] mbox [0:3];
  logic        sstate;  // 0 = accepting, 1 = answering
  logic [ 3:0] scnt;

  localparam logic S_IDLE = 1'b0, S_RESP = 1'b1;

  // The injected bug lives in exactly one term. `HOLD` makes accepting an
  // incoming write conditional on this node having no outgoing write of its
  // own — hold-and-wait, the second Coffman condition, in one wire.
  wire s_free    = (sstate == S_IDLE) && (scnt >= WAIT_STATES[3:0]);
  wire s_allowed = !HOLD || !out_pending;

  assign s_awready = s_free && s_allowed && s_awvalid && s_wvalid;
  assign s_wready  = s_free && s_allowed && s_awvalid && s_wvalid;
  assign s_arready = 1'b0;
  assign s_rvalid  = 1'b0;
  assign s_rdata   = 32'h0;
  assign s_rresp   = OKAY;

  wire [1:0] slot = s_awaddr[3:2];

  always_ff @(posedge aclk or negedge aresetn) begin
    if (!aresetn) begin
      sstate  <= S_IDLE;
      scnt    <= '0;
      s_bvalid <= 1'b0;
      s_bresp  <= OKAY;
      mbox[0] <= 32'h0;
      mbox[1] <= 32'h0;
      mbox[2] <= 32'h0;
      mbox[3] <= 32'h0;
    end else begin
      case (sstate)
        S_IDLE:
          if (s_awvalid && s_wvalid && s_allowed) begin
            if (scnt >= WAIT_STATES[3:0]) begin
              scnt       <= '0;
              mbox[slot] <= s_wdata;
              s_bvalid   <= 1'b1;
              s_bresp    <= OKAY;
              sstate     <= S_RESP;
            end else begin
              scnt <= scnt + 1'b1;
            end
          end else begin
            scnt <= '0;
          end
        S_RESP:
          if (s_bready) begin
            s_bvalid <= 1'b0;
            sstate   <= S_IDLE;
          end
        default: sstate <= S_IDLE;
      endcase
    end
  end

  assign busy = (mstate != M_DONE) || (sstate != S_IDLE);

endmodule
