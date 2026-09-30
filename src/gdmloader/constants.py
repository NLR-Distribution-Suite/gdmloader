"""Pre-built sources. Importing this module performs no network I/O."""

from gdmloader.source import get_gcs_source, get_github_source

GDM_CASE_SOURCE = get_github_source()

GCS_CASE_SOURCE = get_gcs_source()
