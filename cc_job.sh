#!/bin/bash
#SBATCH --account=def-jinguo
#SBATCH --job-name=gg-tkgc
#SBATCH --gres=gpu:v100:2
#SBATCH --cpus-per-task=20
#SBATCH --mem=93G
#SBATCH --time=3-0
#SBATCH --output=./logs/%x-%j.out
#SBATCH --error=./logs/%x-%j.err
#SBATCH --mail-type=ALL
#SBATCH --mail-user=kian.ahrabian@mail.mcgill.ca

module load python/3.7.4 gcc/7.3.0 cuda/10.0.130 cudnn/7.6 openmpi/3.1.2 nccl/2.3.5

source ${VENVDIR}/gg/bin/activate

export LD_LIBRARY_PATH=/home/kahrab/venvs/gg/lib/python3.7/site-packages/torch/lib:${LD_LIBRARY_PATH}
horovodrun -np 2 -hostfile hostfile --mpi-args="--oversubscribe" --timeline-filename ./logs/timeline-${SLURM_JOB_ID}.json --timeline-mark-cycles main.py \
           -ds GitGraph \
           -m TADistMult \
           -d 0.2 \
           -l1 \
           -es 256 \
           -lr 0.001 \
           -e 100 \
           -bs 4096 \
           -ns 64 \
           -f \
           -fp \
           -as \
           -md head \
           -lf 10 \
           -w 10
