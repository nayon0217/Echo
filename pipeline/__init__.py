"""ECHO verification pipeline (policy.md §1).

Text stages built so far:
  stage 2  translate  -> pipeline.translate
  stage 3  route      -> pipeline.router
  stage 4  claims     -> pipeline.claims
  stage 5  queries    -> pipeline.retrieve.generate_queries

Orchestrated by pipeline.pipeline.process_message.
"""
