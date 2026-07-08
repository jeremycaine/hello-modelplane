# Modelplane

## 0. Pre-requisites
If using `podman` and want a `docker` shim.
```
alias docker=podman
```
Good sized podman machine
```
podman machine stop
podman machine set --cpus 8 --memory 16384
podman machine start
```

## 1. Setup vLLM
Install `vllm-metal` on Apple Silicon Macbook and serve a small MLX model.
```bash
curl -fsSL https://raw.githubusercontent.com/vllm-project/vllm-metal/main/install.sh | bash
source ~/.venv-vllm-metal/bin/activate
vllm serve mlx-community/Qwen2.5-0.5B-Instruct-4bit --port 8000
```

Check model is serving. With `vllm-metal` you cannot use `"model":"default"`.

Full output
```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"mlx-community/Qwen2.5-0.5B-Instruct-4bit","messages":[{"role":"user","content":"Say hello in 5 words"}]}'
```
Or just the content field.
```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"mlx-community/Qwen2.5-0.5B-Instruct-4bit","messages":[{"role":"user","content":"Say hello in 5 words"}]}' \
  | jq -r '.choices[0].message.content'
```

## 2. Install Modelplane control plane on kind
Create a kind cluster
```bash
kind create cluster --name modelplane
kubectl cluster-info --context kind-modelplane
```

Install Crossplane.
```bash
helm repo add crossplane-stable https://charts.crossplane.io/stable
helm repo update crossplane-stable
helm install crossplane crossplane-stable/crossplane \
  --namespace crossplane-system --create-namespace \
  --set "args={--enable-dependency-version-upgrades}" --wait

kubectl get pods -n crossplane-system
kubectl get deploy -n crossplane-system
```

Install Modelplane.
```bash
# Pre-create RBAC and provider-helm's ServiceAccount config 
# Crossplane then has the permissions it needs
kubectl apply -f https://docs.modelplane.ai/examples/getting-started/prerequisites.yaml

# Install the Modelplane Configuration package
# Register CRDs and pull in provider and composition functions it depends on
kubectl apply -f - <<'EOF'
apiVersion: pkg.crossplane.io/v1
kind: Configuration
metadata:
  name: modelplane
spec:
  package: xpkg.upbound.io/modelplane/modelplane:v0.1.0
EOF

# Block until dependencies (providers, functions) resolve 
# and the package reports Healthy
kubectl wait configuration/modelplane --for=condition=Healthy --timeout=5m

# check
kubectl get configurationrevisions -o wide
```

## 3. Install gateway 
Use Traefik + MetalLB since `kind` has no cloud load balancer.
```bash
# Find kind's docker subnet so the MetalLB pool is in-range
# usually 172.18.0.0/16
# podman might return 10.89.4.0/24
docker network inspect kind | grep -i subnet     

kubectl apply -f - <<'EOF'
apiVersion: modelplane.ai/v1alpha1
kind: InferenceGateway
metadata:
  name: default
spec:
  backend: Traefik
  traefik:
    version: "40.2.0"
    loadBalancer: MetalLB
    metallb:
      addressPool: "10.89.4.200-10.89.4.250"
EOF

kubectl wait --for=condition=Ready ig/default --timeout=5m
```

## 4. Register the host vLLM as an external endpoint
Skip GPU cluster provisioning. Register the local vLLM server directly as a `ModelEndpoint`, which accepts any OpenAI-compatible URL.

From inside kind, reach the Mac host via `host.docker.internal` (Docker Desktop/OrbStack) or the host's LAN IP (Colima/Podman).

```bash
# Namespace for developer-facing resources (ModelDeployment, ModelService, ModelEndpoint)
kubectl create namespace ml-team

# Manually register the local vLLM server as a ModelEndpoint
# Normally Modelplane creates these itself per-replica, but this
# vLLM server runs outside Modelplane, so we register it directly
kubectl apply -f - <<'EOF'
apiVersion: modelplane.ai/v1alpha1
kind: ModelEndpoint
metadata:
  name: qwen-local
  namespace: ml-team
  labels:
    modelplane.ai/external-provider: local-metal
spec:
  url: http://host.docker.internal:8000/
  rewritePath: /
EOF

# check it's ok
kubectl get modelendpoint -n ml-team
```

## 5. Expose endpoint behind a ModelService
`ModelEndpoint` is not reachable from outside the cluster. Create a `ModelService` to select matching endpoints by label and expose them behind the gateway. In a production setup there could be many replicas across real GPU clusters. This decouples the backend model endpoint from the client-facing call.

Modelplane load-balances across every endpoint that matches the selector, and the same mechanism is used for canary rollouts.
```bash
# Selects the ModelEndpoint by label and exposes it
# through the gateway as one OpenAI-compatible service
kubectl apply -f - <<'EOF'
apiVersion: modelplane.ai/v1alpha1
kind: ModelService
metadata:
  name: qwen
  namespace: ml-team
spec:
  endpoints:
  - selector:
      matchLabels:
        modelplane.ai/external-provider: local-metal
EOF
```

## 6. Podman networking
```bash
# e.g. returns traefik-system
kubectl get svc -A -l app.kubernetes.io/name=traefik

# port forward to reach the gateway from Mac
kubectl port-forward -n traefik-system svc/traefik 8080:80
```

## 7. Test end to end model call
Get the gateway's external IP (the MetalLB-assigned one), then call Modelplane's endpoint. A `ModelService` receives requests at `/<namespace>/<service>/v1/...` and strips only the `/<namespace>/<service>/` prefix. `rewritePath` on the `ModelEndpoint` should point at whatever path the backend actually serves — `/` for a backend already serving at `/v1/...` (like vLLM), or something else if the backend's API lives at a different path.

```bash
curl -v http://localhost:8080/ml-team/qwen/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"mlx-community/Qwen2.5-0.5B-Instruct-4bit","messages":[{"role":"user","content":"Hello from Modelplane"}]}'
```

The request flows: test curl → Modelplane gateway → `ModelService` → `ModelEndpoint` → host `vllm-metal` (Metal GPU) → model → response.

## 8. Test via an LLM client
A minimal LLM client that pulls a Wikipedia summary, sends it to the local Qwen model served through Modelplane (kind + Traefik + vllm-metal), and prints the
model's answer.

If using podman locally ensure port forwarding is set up to reach the Modelplane gateway, e.g.:
`kubectl port-forward -n traefik-system svc/traefik 8080:80`

Example Usage:
```bash
python3 modelplane_agent.py "Earth"
python3 modelplane_agent.py "Australia" --question "List 3 key facts"
```

## References
- [Modelplane getting started](https://docs.modelplane.ai/getting-started/)
- [Route to external providers (`ModelEndpoint`)](https://docs.modelplane.ai/models/model-endpoint/)
- [Register an existing cluster](https://docs.modelplane.ai/platform/inference-cluster/)
- [vllm-metal (Apple Silicon GPU)](https://github.com/vllm-project/vllm-metal)
- [vLLM CPU / Apple Silicon notes](https://docs.vllm.ai/en/stable/getting_started/installation/cpu/)
