module iverilog_dump();
initial begin
    $dumpfile("test_mqnic_sync_dcn.fst");
    $dumpvars(0, mqnic_core_pcie_us);
end
endmodule
