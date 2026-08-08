"""ECHO verification pipeline (policy.md §1).

  stage 1  ASR / vision  -> pipeline.asr / pipeline.vision
  stage 2  translate     -> pipeline.translate
  stage 3  route         -> pipeline.router
  stage 4  claims        -> pipeline.claims
  stage 5–6 retrieve     -> pipeline.retrieve
  stage 7–9 verify       -> pipeline.verify
  stage 10 compose       -> pipeline.compose

Orchestrated by pipeline.pipeline.process_message; traced via pipeline.trace;
served by app.webhook. Evaluation lives in eval/.
"""
