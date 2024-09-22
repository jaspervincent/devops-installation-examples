#docs: https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack
#kubectl create namespace monitoring
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

#helm pull prometheus-community/kube-prometheus-stack

helm install -n monitoring prometheus prometheus-community/kube-prometheus-stack -f values-new.yaml

    #--set defaultRules.rules.etcd=false \
    #--set defaultRules.rules.kubeApiserverAvailability=false


# Update
#helm upgrade --install -n monitoring prometheus prometheus-community/kube-prometheus-stack -f values-new.yaml

# Diff
#helm show values prometheus-community/kube-prometheus-stack >v.yaml; vimdiff v.yaml values-new.yaml

storageclass.yaml
```yaml
cat <<\EOF> gp3.yaml 
kind: StorageClass
apiVersion: storage.k8s.io/v1
metadata:
  name: gp3
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
allowVolumeExpansion: true
provisioner: ebs.csi.aws.com
volumeBindingMode: WaitForFirstConsumer
parameters:
  type: gp3
EOF
```

vaules.yaml
```yaml
prometheus:
  prometheusSpec:
    enableAdminAPI: true
    externalLabels:
      prome_cluster: dev-eks 
    storageSpec:
      volumeClaimTemplate:
        spec:
          resources:
            requests:
              storage: 40Gi
          storageClassName: gp3
    additionalScrapeConfigsSecret: 
      enabled: true
      name: additional-configs
      key: prometheus-additional.yaml
    resources: {}
    # requests:
    #   memory: 400Mi
    nodeSelector: {}
    externalUrl: "https://kubeprometheus.cici.com/"

	
alertmanager:
  alertmanagerSpec:
    configSecret: 'alertmanager-example'
    storage:
      volumeClaimTemplate:
        spec:
          accessModes:
          - ReadWriteOnce
          resources:
            requests:
              storage: 50Gi
          storageClassName: gp3
    externalUrl: https://kubealertmanager.cici.com
	  
grafana:
  adminPassword: prom-operator
  persistence:
    enabled: true
    type: sts
    storageClassName: gp3
    accessModes:
      - ReadWriteOnce
    size: 1Gi
    finalizers:
      - kubernetes.io/pvc-protection
  defaultDashboardsEnabled: false
      

kubeControllerManager:
  enabled: false
kubeEtcd:
  enabled: true
kubeScheduler:
  enabled: false


defaultRules:
  create: true
  rules:
    general: false
    etcd: false
    kubeApiserverAvailability: false
    kubeApiserverBurnrate: false
    kubeApiserverHistogram: false
    kubeApiserverSlos: false
    kubeControllerManager: false
    kubeSchedulerAlerting: false
    kubeSchedulerRecording: false
```
