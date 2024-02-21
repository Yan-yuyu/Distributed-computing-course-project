#!/bin/bash

# 运行脚本
python3 plotting.py --trace_name 120_0.2_5_100_40_25_0,0.5,0.5_0.6,0.3,0.09,0.01_multigpu_dynamic --traces_dir traces/reproduce --pickle_dir reproduce/pickles \
&& python3 plotting.py --trace_name 220_0.2_5_100_25_4_0,0.5,0.5_0.6,0.3,0.09,0.01_multigpu_dynamic --traces_dir traces/reproduce --pickle_dir reproduce/pickles \
&& python3 plotting.py --trace_name 460_0.2_5_100_10_1_0,0.5,0.5_0.6,0.3,0.09,0.01_multigpu_dynamic --traces_dir traces/reproduce --pickle_dir reproduce/pickles \
&& python3 plotting.py --trace_name 900_0.2_5_1000_5_15_0,0.5,0.5_0.6,0.3,0.09,0.01_multigpu_dynamic --traces_dir traces/reproduce --pickle_dir reproduce/pickles

# 创建文件夹
mkdir -p ./bar_avg_jct ./bar_cluster_util ./bar_geometric_mean_jct ./bar_harmonic_mean_jct ./bar_makespan ./bar_unfair_fraction

# 遍历 batch_simulation_figures 文件夹下的每个子文件夹
for folder in ./batch_simulation_figures/*/; do
    # 获取子文件夹名字
    folder_name=$(basename "$folder")

    # 复制文件到对应的同名文件夹中
    cp "$folder/bar_avg_jct.png" "./bar_avg_jct/${folder_name}_bar_avg_jct.png"
    cp "$folder/bar_cluster_util.png" "./bar_cluster_util/${folder_name}_bar_cluster_util.png"
    cp "$folder/bar_geometric_mean_jct.png" "./bar_geometric_mean_jct/${folder_name}_bar_geometric_mean_jct.png"
    cp "$folder/bar_harmonic_mean_jct.png" "./bar_harmonic_mean_jct/${folder_name}_bar_harmonic_mean_jct.png"
    cp "$folder/bar_makespan.png" "./bar_makespan/${folder_name}_bar_makespan.png"
    cp "$folder/bar_unfair_fraction.png" "./bar_unfair_fraction/${folder_name}_bar_unfair_fraction.png"
done
