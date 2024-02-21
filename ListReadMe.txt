运行步骤：

1.创建环境（软件测试文档里有详细步骤说明）

2.运行脚本
位置：shockwave-main/scheduler/reproduce/tacc_32gpus.sh（tacc_64gpus.sh、tacc_128gpus.sh、tacc_256gpus.sh）
作用：运行此脚本，可以自动化循环运行模拟程序，并将不同调度策略的模拟结果输出到相应的文件中，以便后续分析和比较不同策略的性能。

3.运行绘图程序
位置：shockwave-main/scheduler/plotting.py
作用：运行此程序，调整参数（软件测试文档里有具体参数设置，比如若要得到32 个 GPU 集群上120 个作业跟踪场景下调度策略性能对比图：
python3 plotting.py --trace_name 120_0.2_5_100_40_25_0,0.5,0.5_0.6,0.3,0.09,0.01_multigpu_dynamic --traces_dir traces/reproduce --pickle_dir reproduce/pickles），可视化不同调度策略在不同负载下的性能情况。

4.运行整合结果程序
位置：shockwave-main/scheduler/mf.sh
作用：运行此脚本，可以将第2步得到的所有可视化结果按照性能指标重新整合分类，以便分析。

5.查看实验可视化结果
位置1：shockwave-main/scheduler/batch_simulation_figures
位置2：shockwave-main/scheduler/bar_avg_jct（bar_cluster_util、bar_geometric_mean_jct、bar_harmonic_mean_jct、bar_makespan、bar_unfair_fraction）
作用：位置1存储着所有的按照不同负载情况分类的可视化结果图；
	  位置2存储着按照性能指标分类的可视化结果图。