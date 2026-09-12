**`POST /v1/chat/completions` accepts an image.** `content` was typed `str | None` on that
surface, so a multimodal request was a 422 before any of the request ran — on the one endpoint
whose stated purpose is that an OpenAI SDK works unchanged, while `/chat` accepted the same
message and both wires knew how to encode it. It now takes OpenAI's list of content parts as
well as a plain string. The parts stay untyped dictionaries on purpose: the shape is OpenAI's
and it grows, and what Felix does with a part it does not recognise is decided in one place, by
the message validator, rather than by a model that would reject next year's part type.
