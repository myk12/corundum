PROT_RATE_Gbps = 25 #Gbps
GPU_PORT_RATE_Gbps = PROT_RATE_Gbps #Gbps
PORT_NUM = 4 
QUEUE_SIZE_BYTES = 32 * 1024 * 1024  # 64MB 转换为 Bytes
# --- MOE 模型参数 ---
HIDDEN_DIM         = 4096   # 隐藏层维度
BYTES_PER_ELEM     = 2      # BF16
TOKENS_PER_TARGET  = 1024    # 每个 GPU 向每个目标发送的 token 数 如果是256,就是2MB
import math
PAYLOAD_BYTES_PER_TARGET = TOKENS_PER_TARGET * HIDDEN_DIM * BYTES_PER_ELEM
print(f"Payload bytes per target: {PAYLOAD_BYTES_PER_TARGET} Bytes")
#计算PAYLOAD_BYTES_PER_TARGET被port_rate传输需要的时间，单位是ms
payload_transfer_time_us = (PAYLOAD_BYTES_PER_TARGET * 8) / (GPU_PORT_RATE_Gbps * 1e9) * 1000 *1000
print(f"Payload transfer time per target: {payload_transfer_time_us:.2f} Us")
FRAGMENT_PAYLOAD_SIZE = 64 * 1024  # 每个分片的 payload 大小，单位 Bytes
TOTAL_FRAGMENTS=PAYLOAD_BYTES_PER_TARGET//FRAGMENT_PAYLOAD_SIZE
print(f"Total fragments per target: {TOTAL_FRAGMENTS}")
#RTT
# 单个分片的序列化延迟（不是整个 payload）
FRAG_SIZE_BYTES = math.ceil(PAYLOAD_BYTES_PER_TARGET / TOTAL_FRAGMENTS)
FRAG_SERIAL_NS = FRAG_SIZE_BYTES * 8 / PROT_RATE_Gbps

# ACK 包很小，只有以太网头 + 13 字节自定义头，约 27 字节
ACK_SIZE_BYTES = 14 + 13  # Ether header + custom header, 无 payload
ACK_SERIAL_NS = ACK_SIZE_BYTES * 8 / PROT_RATE_Gbps
#打印单个ACK_SERIAL_NS的值
print(f"Frag_SINGLE_NS: {(FRAG_SERIAL_NS/8)*1000} us")
mtu_time_ns = (1025 * 8) / PROT_RATE_Gbps  # 单位是 ns
print(f"MTU_NS: {mtu_time_ns*3} ns")

TIMEOUT_NS = 100 * 1000 * 1000  # 100ms 转换为 ns
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
