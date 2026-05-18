"""Data adapters for Stan's fork.

Each adapter exposes one or more fetch_* functions that produce DataFrames
in a standard schema for the FeaturePipeline. See individual files for
their specific contracts.

Inert adapters (Unusual Whales, Tardis) raise *NotSubscribed exceptions
when called without their respective API keys in .env. The scaffolding
is present so the code imports cleanly; calls fail with clear messages.
"""
