#!/usr/bin/env bash
# Source this before running Python tools manually.
ORANGEPI_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source /opt/ros/noetic/setup.bash
export ROS_MASTER_URI="${ROS_MASTER_URI:-http://127.0.0.1:11311}"
export PYTHONPATH="$ORANGEPI_ROOT/src/orangepi_collect/common:$ORANGEPI_ROOT/../src:${PYTHONPATH:-}"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
export OPENCV_FFMPEG_CAPTURE_OPTIONS='threads;1'
