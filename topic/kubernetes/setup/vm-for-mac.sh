cat <<\EOF> k8s-static-lb.yaml
#cloud-confi
hostname: lb-vip
write_files:
  - path: /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg
    content: |
      network: {config: disabled}
  - path: /etc/netplan/01-static.yaml
    permissions: '0600'
    content: |
      network:
        version: 2
        renderer: networkd
        ethernets:
          enp0s1:
            dhcp4: no
            addresses: [192.168.252.100/24]
            routes:
              - to: default
                via: 192.168.252.1
            nameservers:
              addresses: [223.5.5.5,114.114.114.114]
runcmd:
  - netplan apply
EOF

multipass launch lts --name lb-vip -c 1 -m 1G -d 10G --cloud-init k8s-static-lb.yaml
