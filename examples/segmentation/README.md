# Example: segmentation (BYO-model)
1. `docker build -t .../example-segmentation:0.1.0 examples/segmentation`
2. `helm install sluice charts/sluice` (installs gateway + autoscaler + console)
3. `sluice apply -f examples/segmentation/app.yaml`
4. `curl -X POST http://<gateway>/v1/topwear/infer --data-binary @image.jpg`
   → 200 (warm) or 202+ticket; poll `GET /v1/topwear/status/<ticket>`.
5. Open the Console to watch the queue + worker states (and pause/resume).
Swap `handler.py` for a real GPU model to go to production.
