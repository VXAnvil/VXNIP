ARG NVKE_NODE_IMAGE=cwe-capd-kindest-node:local
FROM ${NVKE_NODE_IMAGE}

RUN if ! (command -v sshd && command -v sudo && command -v python3 && test -x /usr/bin/cloud-init); then \
      apt-get -o Acquire::Retries=3 update && \
      apt-get -o Acquire::Retries=3 install -y --no-install-recommends openssh-server sudo python3 cloud-init; \
    fi && rm -rf /var/lib/apt/lists/*
# Kind's entrypoint regenerates machine-id on every container start. VXNIP
# clears it once below, then keeps the first generated identity across restarts.
RUN python3 -c 'from pathlib import Path; p=Path("/usr/local/bin/entrypoint"); s=p.read_text(); needle="fix_machine_id() {\n"; guard="  if grep -Eq \"^[0-9a-f]{32}$\" /etc/machine-id; then return; fi\n"; assert s.count(needle)==1; p.write_text(s if guard in s else s.replace(needle, needle+guard))'
RUN rm -f /usr/local/bin/cloud-init /etc/default/nke-agent /etc/default/nvke-agent \
      /var/lib/nvke/state.json /etc/systemd/system/spire-agent.service.d/override.conf \
      /etc/ssh/ssh_host_* /var/lib/dbus/machine-id && \
    rm -f /var/lib/cloud/seed/nocloud/* && \
    truncate -s 0 /etc/machine-id && ln -s /etc/machine-id /var/lib/dbus/machine-id && \
    install -d -m 0750 -o spire -g spire /var/lib/spire-agent && \
    install -d /var/lib/cloud/seed/nocloud /etc/cloud/cloud.cfg.d \
      /etc/systemd/system/ssh.service.d /etc/systemd/system/nvke-agent.service.d && \
    printf '%s\n' 'instance-id: vxnip-nvke-docker' > /var/lib/cloud/seed/nocloud/meta-data && \
    printf '%s\n' '#cloud-config' 'manage_etc_hosts: false' > /var/lib/cloud/seed/nocloud/user-data && \
    printf '%s\n' 'datasource_list: [NoCloud]' 'network: {config: disabled}' 'preserve_hostname: true' \
      'ssh_deletekeys: false' 'cloud_init_modules: []' 'cloud_config_modules: []' 'cloud_final_modules: []' \
      > /etc/cloud/cloud.cfg.d/99-vxnip.cfg && \
    systemctl disable nvke-agent.service spire-agent.service && \
    ln -sfn nvke-agent.service /etc/systemd/system/nke-agent.service && \
    printf '%s\n' '[Service]' 'ExecStart=' 'ExecStart=/usr/local/bin/nvke-agent $AGENT_ARGS --disable-mlx5-rescan' \
      > /etc/systemd/system/nvke-agent.service.d/95-container-no-mlx5.conf && \
    if ! id vxnip >/dev/null 2>&1; then useradd -m -s /bin/bash vxnip; fi && passwd -d vxnip && \
    install -d -m 0700 -o vxnip -g vxnip /home/vxnip/.ssh && \
    printf '%s\n' 'vxnip ALL=(root) NOPASSWD: /usr/local/sbin/vxnip-kubeadm ""' > /etc/sudoers.d/vxnip && \
    chmod 0440 /etc/sudoers.d/vxnip && visudo -cf /etc/sudoers.d/vxnip && \
    printf '%s\n' 'AuthenticationMethods publickey' 'PasswordAuthentication no' 'KbdInteractiveAuthentication no' \
      'PermitRootLogin no' > /etc/ssh/sshd_config.d/01-vxnip.conf && \
    printf '%s\n' '[Service]' 'ExecStartPre=' 'ExecStartPre=/usr/bin/ssh-keygen -A' 'ExecStartPre=/usr/sbin/sshd -t' \
      > /etc/systemd/system/ssh.service.d/10-host-keys.conf && systemctl enable ssh.service
COPY --chmod=0755 scripts/vxnip-kubeadm /usr/local/sbin/vxnip-kubeadm

RUN chmod 0755 /usr/bin /usr/local/bin && test -x /usr/bin/kubeadm
