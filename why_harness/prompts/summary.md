You are given one section of a Chinese textbook or exam paper. Write a summary, a one-line brief, and a list of keywords for it.

All output values (summary, brief, keywords) MUST be written in Chinese, because the books are in Chinese. These instructions are in English, but none of your output should be in English unless the body itself uses English terms.

## What the body looks like

The body comes in one of two shapes:

(a) Leaf section: the actual text of the section. It may be cut off mid-sentence because it is truncated to 6000 characters. Summarize what is there and do not guess at what is missing.

(b) Internal node (a chapter or part that contains sub-sections): a synthesized body in exactly this shape:
【导语】
<intro text, possibly empty>
【下属各部分摘要】
- <child title>: <child summary>
- ...
For this shape, summarize the node as a whole. Say what the part covers overall and how its sub-parts fit together, using the intro if it has one. Do not just list or join the child summaries one by one. The markers 【导语】 and 【下属各部分摘要】 are structure only. Never mention them, and never use them as keywords.

## Output format

Output exactly one JSON object and nothing else: no explanation, no prose before or after it, no code fences. Use exactly these key names:

{"summary": "……", "brief": "……", "keywords": ["……", "……"]}

Always include all three keys.

## Requirements

1. summary: Chinese, a single paragraph, at most {{limit}} characters (anything longer is cut off mechanically, so stay within the limit). The reader is an educator. Explain clearly what this section covers, in enough detail that the summary can stand in for the original text in context. Do not copy formulas, equations, or long verbatim passages. Describe them in words.

2. brief: Chinese, exactly ONE sentence, at most {{brief_limit}} characters. It is used as a one-line table-of-contents entry for an LLM, so state only WHAT the section covers. Do not evaluate it, give details, examples, or numbers, or use filler such as "本节" or "本章". Use no punctuation at the end except an optional Chinese period 。, and no internal punctuation beyond what the sentence needs. Never leave brief out and never go over the limit. Count the characters before you write it.

3. keywords: 3 to 6 items. Each item MUST be a contiguous fragment that appears verbatim in the body above, 2 to 12 characters long, and preferably a proper noun, concept name, or question-type name. Copy each one character for character. Do not split a term apart, merge separate pieces, paraphrase, translate, normalize, or invent terms. Any keyword that is not an exact substring of the body is discarded. For internal nodes, keywords may come from the intro, the child titles, or the child summaries.

4. If the body is empty or contains only placeholders (for example, only the structure markers with nothing meaningful under them), output exactly:
{"summary": "", "keywords": []}

Title: {{title}}

Body:
{{body}}
