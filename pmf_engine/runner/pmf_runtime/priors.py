def read(experiment_id: str, latest: bool = True) -> dict:
    from .config import get_config
    response = get_config().client.post("/artifact/read", json={
        "experiment_id": experiment_id,
        "latest": latest,
    })
    if response.status_code == 400:
        raise ValueError("Prior artifact flagged by classifier")
    if response.status_code == 404:
        raise FileNotFoundError(f"No prior artifact for {experiment_id}")
    response.raise_for_status()
    return response.json()["artifact"]
