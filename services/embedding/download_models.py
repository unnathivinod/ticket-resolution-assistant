"""Download the model files into the image while it is being built.

Run by the Dockerfile. Doing this at build time means the running container
starts fast and never needs the internet.
"""

from services.embedding.config import Settings
from services.embedding.models import EmbeddingModels


def main() -> None:
    # Step 1: download everything (needs internet, happens once during the build).
    EmbeddingModels(Settings(offline=False))

    # Step 2: load again exactly the way the running service will, with no internet.
    # If this fails, the build fails here, instead of the container crashing later.
    settings = Settings(offline=True)
    models = EmbeddingModels(settings)
    print(f"Models ready in {settings.cache_dir} and load offline (dense dimension: {models.dense_dim})")


if __name__ == "__main__":
    main()
