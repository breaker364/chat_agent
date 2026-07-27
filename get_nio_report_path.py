import os

# 获取绝对路径
file_path = "tmp/nio_analysis_report.md"
absolute_path = os.path.abspath(file_path)
print(absolute_path)