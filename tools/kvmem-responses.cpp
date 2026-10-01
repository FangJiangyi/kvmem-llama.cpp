#include "kvmem-responses.h"

// This TU deliberately includes only upstream server headers, so `json` here is
// common_json. See kvmem-responses.h for why the conversion crosses a string.
#include "server-chat.h"

#include <stdexcept>
#include <string>
#include <vector>

// A reasoning item a client sends back may carry its text only under `summary`.
//
// We emit every reasoning item with the text in both `summary` (type
// `summary_text`) and `content` (type `reasoning_text`), because OpenAI defines
// both and the two serve different readers. But @ai-sdk/openai keeps only the
// summary: when it replays the conversation it sends back
//
//     {"type": "reasoning", "summary": [{"type": "summary_text", "text": ...}]}
//
// with no `content` at all. server_chat_convert_responses_to_chatcmpl reads only
// `content[0].text` and rejects the item outright -- "item['content'] is not an
// array" -- which fails the whole request, tool loop included.
//
// Fold the summary into `content` before handing the body over, so upstream sees
// the shape it expects and the model keeps the reasoning it produced last turn
// instead of losing it. Items that already carry a `content` array are left
// untouched: that is the shape OpenAI's own clients send, and upstream handles it.
static void kvmem_responses_fold_reasoning_summary(json & body) {
    if (!body.contains("input") || !body.at("input").is_array()) {
        return;
    }
    for (json & item : body.at("input")) {
        if (!item.is_object() ||
            json_value(item, "type", std::string()) != "reasoning" ||
            item.contains("content")) {
            continue;
        }
        if (!item.contains("summary") || !item.at("summary").is_array()) {
            continue;
        }
        json content = json::array();
        for (const json & part : item.at("summary")) {
            if (part.is_object() && part.contains("text") && part.at("text").is_string()) {
                content.push_back(json{
                    {"text", part.at("text")},
                    {"type", "reasoning_text"},
                });
            }
        }
        if (content.empty()) {
            // Nothing worth keeping; give upstream the empty array its own check
            // wants so it reports the clearer "item['content'] is empty" instead
            // of failing on a missing key.
            content.push_back(json{{"text", ""}, {"type", "reasoning_text"}});
        }
        item["content"] = content;
    }
}

// A plain assistant message a client replays may arrive without a `type`.
//
// Upstream recognises an output message by `role == "assistant"` *and*
// `type == "message"`:
//
//     } else if (exists_and_is_string(item, "role") &&
//         item.at("role") == "assistant" &&
//         exists_and_is_string(item, "type") &&
//         item.at("type") == "message"
//
// @ai-sdk/openai replays the assistant turn as {"role": "assistant", "content":
// "..."} with no `type` at all, so that branch does not match, the item falls
// through every remaining branch, and the request dies on the final
// "Cannot determine type of 'item'". Supply the type it is looking for. The
// user/system/developer branch above matches on `role` alone, so those are left
// as they are.
static void kvmem_responses_fill_message_type(json & body) {
    if (!body.contains("input") || !body.at("input").is_array()) {
        return;
    }
    for (json & item : body.at("input")) {
        if (item.is_object() &&
            !item.contains("type") &&
            json_value(item, "role", std::string()) == "assistant") {
            item["type"] = "message";
        }
    }
}

// Fold the system turn a client sends inside `input` into the one `instructions`
// produces.
//
// A request may carry its system prompt in either place, or both. Upstream turns
// `instructions` into a leading system message and passes a system/developer item
// through untouched, so both arrive at the chat template:
//
//     [system] [user] [system]
//
// It then rewrites `developer` to `system` unconditionally, and the only merging
// workaround it has -- system_message_not_supported -- is gated on the template
// *not* declaring support for a system role, i.e. exactly the case that never
// needs it. The hybrid models this server targets therefore get two system
// messages, and their template raises
//
//     'System message must be at the beginning.'
//
// for every request that sets `instructions` and also replays a system/developer
// turn in `input`. Join the two texts into the leading system turn and drop the
// later one, so the template sees exactly one, first.
//
// The text is joined rather than one side winning: `instructions` is usually the
// client's own preamble while the `input` item is the developer message it wants
// preserved, and silently discarding either would change what the model is told.
static void kvmem_responses_merge_system_turns(json & body) {
    if (!body.contains("input") || !body.at("input").is_array()) {
        return;
    }

    const auto is_system_turn = [](const json & item) {
        const std::string role = json_value(item, "role", std::string());
        return role == "system" || role == "developer";
    };
    // Upstream renders a string `content` as-is; a graded one arrives as a list.
    const auto item_text = [](const json & item) {
        std::string text;
        for (const char * key : {"content", "text"}) {
            if (item.contains(key) && item.at(key).is_string()) {
                const std::string part = item.at(key).get<std::string>();
                text += (text.empty() ? "" : "\n\n") + part;
            }
        }
        return text;
    };

    std::vector<std::string> prompts;
    if (body.contains("instructions") && body.at("instructions").is_string()) {
        const std::string instructions = body.at("instructions").get<std::string>();
        if (!instructions.empty()) {
            prompts.push_back(instructions);
        }
    }
    for (const json & item : body.at("input")) {
        if (item.is_object() && is_system_turn(item)) {
            const std::string text = item_text(item);
            if (!text.empty()) {
                prompts.push_back(text);
            }
        }
    }

    if (prompts.empty()) {
        // Nothing to merge; leave the body alone so upstream keeps reporting its
        // own errors for a system turn with no text.
        return;
    }

    std::string merged;
    for (const std::string & prompt : prompts) {
        merged += (merged.empty() ? "" : "\n\n") + prompt;
    }
    body["instructions"] = merged;

    json kept = json::array();
    for (json & item : body.at("input")) {
        if (item.is_object() && is_system_turn(item)) {
            // Any images the turn carried would be dropped with it; such a
            // request is not one this bridge supports.
            for (const json & content : item.value("content", json::array())) {
                if (content.is_object() && json_value(content, "type", std::string()) != "text") {
                    throw std::runtime_error("system message with non-text content is not supported");
                }
            }
            continue;
        }
        kept.push_back(std::move(item));
    }
    body["input"] = std::move(kept);
}

std::string kvmem_responses_to_chatcmpl(const std::string & body) {
    json parsed = json::parse(body);
    kvmem_responses_fold_reasoning_summary(parsed);
    kvmem_responses_fill_message_type(parsed);
    kvmem_responses_merge_system_turns(parsed);
    return server_chat_convert_responses_to_chatcmpl(parsed).dump();
}
