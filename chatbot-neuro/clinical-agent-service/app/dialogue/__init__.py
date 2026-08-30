"""Deterministic conversation machinery: the frame, reference resolution, slot validation.

Everything in this package is application code, not model output. The division of labour it
enforces is the one the architecture already asks for and had never been given a home: the
interpreter reads *language*, this package decides *state*. A model that misreads a sentence
costs a clarifying question; it cannot advance a task, fill a slot it did not corroborate, or
skip a validation rule, because none of those live in the model.
"""
