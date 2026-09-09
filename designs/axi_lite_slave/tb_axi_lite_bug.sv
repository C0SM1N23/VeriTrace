`timescale 1ns / 1ps

// §14.1 reference bug. This minimal AXI4-Lite master changes AWADDR on every
// stalled cycle instead of holding the payload stable until AWREADY accepts it.
module tb_axi_lite_bug;
    logic        aclk = 1'b0;
    logic        aresetn = 1'b0;
    logic        awvalid = 1'b0;
    logic        awready = 1'b0;
    logic [31:0] awaddr = 32'h1000;
    logic [ 2:0] awprot = '0;
    logic        wvalid = 1'b0;
    logic        wready = 1'b0;
    logic [31:0] wdata = 32'hCAFE_BABE;
    logic [ 3:0] wstrb = 4'hF;
    logic        bvalid = 1'b0;
    logic        bready = 1'b1;
    logic [ 1:0] bresp = 2'b00;
    logic        arvalid = 1'b0;
    logic        arready = 1'b0;
    logic [31:0] araddr = '0;
    logic [ 2:0] arprot = '0;
    logic        rvalid = 1'b0;
    logic        rready = 1'b1;
    logic [31:0] rdata = '0;
    logic [ 1:0] rresp = 2'b00;
    logic [ 3:0] step = '0;

    always #5 aclk = ~aclk;

    always_ff @(posedge aclk or negedge aresetn) begin
        if (!aresetn) begin
            awaddr <= 32'h1000;
            step <= '0;
        end else if (awvalid && !awready) begin
            // BUG: AXI requires AWADDR to remain stable in this interval.
            step <= step + 1'b1;
            awaddr <= 32'h1000 + {26'd0, step, 2'b00};
        end
    end

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, tb_axi_lite_bug);
        #12 aresetn = 1'b1;
        #8 awvalid = 1'b1;
        repeat (5) @(posedge aclk);
        awready = 1'b1;
        @(posedge aclk);
        awvalid = 1'b0;
        awready = 1'b0;
        #20 $finish;
    end
endmodule
