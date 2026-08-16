"""Physician Expertise Explorer — data ingestion pipeline.

Pulls physician identity, education, publications, clinical trials and condition
vocabulary from public sources into PostgreSQL, which is the contract between
this repo and the serving application (ADR 0003).
"""

__version__ = "0.1.0"
