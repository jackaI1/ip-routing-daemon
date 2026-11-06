FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends g++ make python3 iproute2 iputils-ping perl && rm -rf /var/lib/apt/lists/*
WORKDIR /lab
COPY . .
RUN make all unit && prove -l t && python3 -m unittest discover -s tests -v
CMD ["python3", "lab/netns.py"]
