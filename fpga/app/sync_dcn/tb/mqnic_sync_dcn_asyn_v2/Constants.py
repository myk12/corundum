PROT_RATE_Gbps = 10 #Gbps
HOP_DELAY_NS = 600   
QUEUE_SIZE_BYTES = 32 * 1024 * 1024  # 64MB 转换为 Bytes
# --- MOE 模型参数 ---
HIDDEN_DIM         = 4096   # 隐藏层维度
BYTES_PER_ELEM     = 2      # BF16
TOKENS_PER_TARGET  = 1024    # 每个 GPU 向每个目标发送的 token 数 如果是256,就是2MB

PAYLOAD_BYTES_PER_TARGET = TOKENS_PER_TARGET * HIDDEN_DIM * BYTES_PER_ELEM
#RTT
SERIALIZATION_NS = int(PAYLOAD_BYTES_PER_TARGET * 8 / PROT_RATE_Gbps)  # 41943ns
RTT_NS = 2 * (SERIALIZATION_NS*6 + 4 * HOP_DELAY_NS)                     # ~88686ns
TIMEOUT_NS = 3 * RTT_NS  
print(f"RTT: {RTT_NS / 1000000} ms, Timeout: {TIMEOUT_NS} ns")
spine3_leaf1_routing_table = {
    1: 17, 2: 17,  # 目标 FPGA1, FPGA2 -> 走 Port 17 
    3: 18, 4: 18,  # 目标 FPGA3, FPGA4 -> 走 Port 18 
    5: 19, 6: 19,  # 目标 FPGA5, FPGA6 -> 走 Port 19 
    7: 20, 8: 20   # 目标 FPGA7, FPGA8 -> 走 Port 20 
}


spine3_leaf2_routing_table = {
    1: 21, 2: 21,  # 目标 FPGA1, FPGA2 -> 走 Port 21 
    3: 22, 4: 22,  # 目标 FPGA3, FPGA4 -> 走 Port 22 
    5: 23, 6: 23,  # 目标 FPGA5, FPGA6 -> 走 Port 23 
    7: 24, 8: 24   # 目标 FPGA7, FPGA8 -> 走 Port 24 
}

# Leaf 1 (Ports 1~4)
spine1_leaf1_routing_table = {
    1: 2, 2: 4,                                  
    3: 1, 4: 3, 5: [1, 3], 7: [1, 3],  
    6: [3, 1], 8: [3, 1]
}

# Leaf 2 (Ports 5~8)
spine1_leaf2_routing_table = {
    3: 6, 4: 8,                                  
    1: 5, 2: 7, 5: [5, 7], 7: [5, 7],  
    6: [7, 5], 8: [7, 5]
}

# Leaf 3 (Ports 9~12)
spine2_leaf1_routing_table = {
    5: 10, 6: 12,                                      
    1: [9, 11], 3: [9, 11], 2: [11, 9], 4: [11, 9],
    7: 9, 8: 11
}

# Leaf 4 (Ports 13~16)
spine2_leaf2_routing_table = {
    7: 14, 8: 16,                                      
    1: [13, 15], 3: [13, 15], 2: [15, 13], 4: [15, 13],
    5: 13, 6: 15
}
port_mapping = {
    1:17,3:21,5:18,7:22,9:19,11:23,13:20,15:24,
    17:1,21:3,18:5,22:7,19:9,23:11,20:13,24:15
}
