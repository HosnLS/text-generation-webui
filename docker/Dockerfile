FROM pytorch/pytorch:2.0.0-cuda11.7-cudnn8-runtime

USER root
#RUN apt-get remove libappstream3 -y
RUN apt-get update  \
    && DEBIAN_FRONTEND=noninteractive TZ=Asia/Shanghai apt-get install -y \
    git cmake build-essential tzdata x11vnc python3-pip htop tmux git \
    && conda remove cmake -y \
    && pip install --upgrade pip setuptools wheel \
    && pip config set global.index-url https://mirror.sjtu.edu.cn/pypi/web/simple \
    && apt-get autoremove --purge && apt-get clean \
    && conda clean --all -y \
    && pip cache purge \
    && rm -rf /tmp/* /var/tmp/*

# docker build -t "hosnls:textgen" .

#mkdir /home/ljj/nfs/Project/text-generation-webui
#mkdir /home/ljj/nfs/Project/text-generation-webui/models
#
#
docker run -itd --init --name textgen --gpus=all --ipc=host --net=host \
 --mount type=bind,source=/home/ljj/nfs/Project/textgen,target=/home/ljj/nfs/Project/textgen \
 hosnls:textgen /bin/bash
##   --user="$(id -u):$(id -g)" \


