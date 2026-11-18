ARG NVKE_NODE_IMAGE=cwe-capd-kindest-node:local
FROM ${NVKE_NODE_IMAGE}

RUN apt-get update && apt-get install -y --no-install-recommends openssh-server sudo python3 cloud-init && rm -rf /var/lib/apt/lists/*
RUN rm -f /usr/local/bin/cloud-init /etc/default/nke-agent /etc/default/nvke-agent \
      /var/lib/nvke/state.json /etc/systemd/system/spire-agent.service.d/override.conf \
      /etc/ssh/ssh_host_* /var/lib/dbus/machine-id && \
    truncate -s 0 /etc/machine-id && ln -s /etc/machine-id /var/lib/dbus/machine-id && \
    install -d -m 0750 -o spire -g spire /var/lib/spire-agent && \
    install -d /var/lib/cloud/seed/nocloud /etc/cloud/cloud.cfg.d \
      /etc/systemd/system/ssh.service.d /etc/systemd/system/nvke-agent.service.d && \
    printf '%s\n' 'instance-id: homelab-nvke-docker' > /var/lib/cloud/seed/nocloud/meta-data && \
    printf '%s\n' '#cloud-config' 'manage_etc_hosts: false' > /var/lib/cloud/seed/nocloud/user-data && \
    printf '%s\n' 'datasource_list: [NoCloud]' 'network: {config: disabled}' 'preserve_hostname: true' \
      'ssh_deletekeys: false' 'cloud_init_modules: []' 'cloud_config_modules: []' 'cloud_final_modules: []' \
      > /etc/cloud/cloud.cfg.d/99-homelab.conf && \
    systemctl disable nvke-agent.service spire-agent.service && \
    ln -sfn nvke-agent.service /etc/systemd/system/nke-agent.service && \
    printf '%s\n' '[Service]' 'ExecStart=' 'ExecStart=/usr/local/bin/nvke-agent $AGENT_ARGS --disable-mlx5-rescan' \
      > /etc/systemd/system/nvke-agent.service.d/95-container-no-mlx5.conf && \
    useradd -m -s /bin/bash homelab && passwd -d homelab && \
    install -d -m 0700 -o homelab -g homelab /home/homelab/.ssh && \
    printf '%s\n' 'homelab ALL=(root) NOPASSWD: /usr/local/sbin/homelab-kubeadm ""' > /etc/sudoers.d/homelab && \
    chmod 0440 /etc/sudoers.d/homelab && visudo -cf /etc/sudoers.d/homelab && \
    printf '%s\n' 'AuthenticationMethods publickey' 'PasswordAuthentication no' 'KbdInteractiveAuthentication no' \
      'PermitRootLogin no' > /etc/ssh/sshd_config.d/01-homelab.conf && \
    printf '%s\n' '[Service]' 'ExecStartPre=' 'ExecStartPre=/usr/bin/ssh-keygen -A' 'ExecStartPre=/usr/sbin/sshd -t' \
      > /etc/systemd/system/ssh.service.d/10-host-keys.conf && systemctl enable ssh.service
COPY --chmod=0755 scripts/homelab-kubeadm /usr/local/sbin/homelab-kubeadm
