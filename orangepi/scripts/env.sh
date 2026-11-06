#!/usr/bin/env bash
# Source this before running Python tools manually.
ORANGEPI_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$ORANGEPI_ROOT/src/orangepi_collect/common:$ORANGEPI_ROOT/../src"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
export OPENCV_FFMPEG_CAPTURE_OPTIONS='threads;1'
unset ROS_MASTER_URI ROS_PACKAGE_PATH ROS_DISTRO ROS_VERSION ROS_PYTHON_VERSION ROSLISP_PACKAGE_DIRECTORIES
