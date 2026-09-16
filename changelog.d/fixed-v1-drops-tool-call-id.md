**`POST /v1/chat/completions` validated `name` and `tool_call_id` and then discarded them.**
Only `role` and `content` were forwarded, so an OpenAI SDK doing the standard tool round-trip
sent a result whose id was dropped and the model received a tool message answering nothing in
particular. Both fields now reach the message the model sees.
