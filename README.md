# Feedback Pulse — Helm chart repository

This branch is a [Helm chart repository](https://helm.sh/docs/topics/chart_repository/) index,
kept separate from `main` (the app source).

Add it with:

```bash
helm repo add feedback-pulse https://raw.githubusercontent.com/revanthky/feedback-pulse/helm-repo
helm repo update
helm install feedback-pulse feedback-pulse/feedback-pulse
```

`index.yaml` and `feedback-pulse-0.1.0.tgz` are regenerated from `charts/feedback-pulse` on
`main` via:

```bash
helm package charts/feedback-pulse -d <out>
helm repo index <out> --url https://raw.githubusercontent.com/revanthky/feedback-pulse/helm-repo --merge index.yaml
```
