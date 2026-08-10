`timescale 1ns / 1ps
//
// The DMA path of §8.19: an AXI4-Lite slave port in, an AXI4-Lite master port
// out, one transaction in flight at a time. Store-and-forward rather than a
// wire-through, because a bridge that merely renamed the nets would not be two
// interfaces — the trace would show one bus under two names, and there would be
// no "path" for a corruption to happen *in*.
//
// The injected bug is one line, and it is the one §8.19 describes: a byte-enable
// mask that loses two bits. Everything else forwards unchanged, so the address
// sequence on both ports is identical and the two are directly comparable —
// which is exactly what makes VeriTrace able to say *where* the data changed and
// not merely that it did.
//
// Compile with -DNO_CORRUPTION for the same bridge with the bug gone.
//
module dma_bridge (
    input  logic        aclk,
    input  logic        aresetn,

    // ---- slave port: the CPU side -----------------------------------------
    input  logic        s_axi_awvalid,
    output logic        s_axi_awready,
    input  logic [31:0] s_axi_awaddr,
    input  logic [ 2:0] s_axi_awprot,
    input  logic        s_axi_wvalid,
    output logic        s_axi_wready,
    input  logic [31:0] s_axi_wdata,
    input  logic [ 3:0] s_axi_wstrb,
    output logic        s_axi_bvalid,
    input  logic        s_axi_bready,
    output logic [ 1:0] s_axi_bresp,
    input  logic        s_axi_arvalid,
    output logic        s_axi_arready,
    input  logic [31:0] s_axi_araddr,
    input  logic [ 2:0] s_axi_arprot,
    output logic        s_axi_rvalid,
    input  logic        s_axi_rready,
    output logic [31:0] s_axi_rdata,
    output logic [ 1:0] s_axi_rresp,

    // ---- master port: the memory side -------------------------------------
    output logic        m_axi_awvalid,
    input  logic        m_axi_awready,
    output logic [31:0] m_axi_awaddr,
    output logic [ 2:0] m_axi_awprot,
    output logic        m_axi_wvalid,
    input  logic        m_axi_wready,
    output logic [31:0] m_axi_wdata,
    output logic [ 3:0] m_axi_wstrb,
    input  logic        m_axi_bvalid,
    output logic        m_axi_bready,
    input  logic [ 1:0] m_axi_bresp,
    output logic        m_axi_arvalid,
    input  logic        m_axi_arready,
    output logic [31:0] m_axi_araddr,
    output logic [ 2:0] m_axi_arprot,
    input  logic        m_axi_rvalid,
    output logic        m_axi_rready,
    input  logic [31:0] m_axi_rdata,
    input  logic [ 1:0] m_axi_rresp
);

  // The byte-enable mask the write path applies on its way through. 4'b1111 is
  // the whole point of a pass-through bridge; 4'b1100 is the bug.
`ifdef NO_CORRUPTION
  localparam logic [3:0] STRB_MASK = 4'b1111;
`else
  localparam logic [3:0] STRB_MASK = 4'b1100;
`endif

  // ---- write path ---------------------------------------------------------

  typedef enum logic [1:0] {W_IDLE, W_ISSUE, W_WAIT, W_RESP} wstate_e;
  wstate_e wstate;

  logic [31:0] waddr_q, wdata_q;
  logic [ 3:0] wstrb_q;
  logic [ 2:0] wprot_q;
  logic [ 1:0] bresp_q;

  assign s_axi_awready = (wstate == W_IDLE) && s_axi_awvalid && s_axi_wvalid;
  assign s_axi_wready  = s_axi_awready;
  assign s_axi_bvalid  = (wstate == W_RESP);
  assign s_axi_bresp   = bresp_q;

  assign m_axi_awvalid = (wstate == W_ISSUE);
  assign m_axi_wvalid  = (wstate == W_ISSUE);
  assign m_axi_awaddr  = waddr_q;
  assign m_axi_awprot  = wprot_q;
  assign m_axi_wdata   = wdata_q;
  assign m_axi_wstrb   = wstrb_q;
  assign m_axi_bready  = (wstate == W_WAIT);

  always_ff @(posedge aclk or negedge aresetn) begin
    if (!aresetn) begin
      wstate  <= W_IDLE;
      waddr_q <= 32'h0;
      wdata_q <= 32'h0;
      wstrb_q <= 4'h0;
      wprot_q <= 3'h0;
      bresp_q <= 2'b00;
    end else begin
      case (wstate)
        W_IDLE:
        if (s_axi_awvalid && s_axi_wvalid) begin
          waddr_q <= s_axi_awaddr;
          wprot_q <= s_axi_awprot;
          wdata_q <= s_axi_wdata;
          // The whole bug: the mask is applied to the byte enables and not to
          // the data, so the word still looks right on this side of the flop
          // and only the lanes that reach memory are short.
          wstrb_q <= s_axi_wstrb & STRB_MASK;
          wstate  <= W_ISSUE;
        end
        W_ISSUE: if (m_axi_awready && m_axi_wready) wstate <= W_WAIT;
        W_WAIT:
        if (m_axi_bvalid) begin
          bresp_q <= m_axi_bresp;
          wstate  <= W_RESP;
        end
        W_RESP: if (s_axi_bready) wstate <= W_IDLE;
        default: wstate <= W_IDLE;
      endcase
    end
  end

  // ---- read path ----------------------------------------------------------

  typedef enum logic [1:0] {R_IDLE, R_ISSUE, R_WAIT, R_DATA} rstate_e;
  rstate_e rstate;

  logic [31:0] raddr_q, rdata_q;
  logic [ 2:0] rprot_q;
  logic [ 1:0] rresp_q;

  assign s_axi_arready = (rstate == R_IDLE) && s_axi_arvalid;
  assign s_axi_rvalid  = (rstate == R_DATA);
  assign s_axi_rdata   = rdata_q;
  assign s_axi_rresp   = rresp_q;

  assign m_axi_arvalid = (rstate == R_ISSUE);
  assign m_axi_araddr  = raddr_q;
  assign m_axi_arprot  = rprot_q;
  assign m_axi_rready  = (rstate == R_WAIT);

  always_ff @(posedge aclk or negedge aresetn) begin
    if (!aresetn) begin
      rstate  <= R_IDLE;
      raddr_q <= 32'h0;
      rdata_q <= 32'h0;
      rprot_q <= 3'h0;
      rresp_q <= 2'b00;
    end else begin
      case (rstate)
        R_IDLE:
        if (s_axi_arvalid) begin
          raddr_q <= s_axi_araddr;
          rprot_q <= s_axi_arprot;
          rstate  <= R_ISSUE;
        end
        R_ISSUE: if (m_axi_arready) rstate <= R_WAIT;
        R_WAIT:
        if (m_axi_rvalid) begin
          rdata_q <= m_axi_rdata;
          rresp_q <= m_axi_rresp;
          rstate  <= R_DATA;
        end
        R_DATA: if (s_axi_rready) rstate <= R_IDLE;
        default: rstate <= R_IDLE;
      endcase
    end
  end

endmodule
