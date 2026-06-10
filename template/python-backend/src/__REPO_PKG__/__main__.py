"""Run the API with uvicorn (`python -m __REPO_PKG__`)."""

import uvicorn


def main() -> None:
    """Start the ASGI server on the nuklaut ingress port."""
    uvicorn.run("__REPO_PKG__.api:app", host="0.0.0.0", port=8080)  # noqa: S104


if __name__ == "__main__":
    main()
