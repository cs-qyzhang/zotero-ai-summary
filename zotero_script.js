/**
 * Generate paper summary using LLM
 * @author Qiuyang Zhang
 * @usage https://github.com/cs-qyzhang/zotero-ai-summary
 */

/************* Configurations Start *************/
// Server URL used to parse PDF and convert Markdown to HTML
let serverUrl = "https://paper_summarizer.jianyue.tech";
// Set this to true if you manage PDFs as "Link to File" using ZotMoov or ZotFile. Otherwise, set it to false
let only_link_file = false;
// Used in conjunction with ZotMoov or ZotFile. Specifies the maximum number of seconds to wait after adding a paper to check if the PDF download is complete.
let timeout = 30;
// OpenAI-compatible API base URL
let openaiBaseUrl = "https://dashscope.aliyuncs.com/compatible-mode/v1";
// Gemini
// let openaiBaseUrl = "https://generativelanguage.googleapis.com/v1beta/openai/";
// Model name
let modelName = "qwen-plus-latest";
// API key
let apiKey = "sk-xxxxxxxxxxxxx";
// Model's max context length
let chunkSize = 64000;
// The overlap between chunks of text to process
let chunkOverlap = 1000;
// Header of generated note
let headerTemplate = "<h2>AI Generated Summary ({{modelName}})</h2>"

// The following prompts are used to generate the summary. You can modify them
// to change the summary language and style. The simplest way is translate the
// prompts to your language. {title} and {text} are placeholders for the actual
// title and text of the paper.

// Prompt for "stuff" method, which is used when there is only one split
let stuff_prompt = `Below is an excerpt from a research paper titled "{title}".
Text excerpt:
--- START OF PAPER ---
{text}
--- END OF PAPER ---

Please analyze this text and provide a concise summary. Structure your response using the following points:

1. **Context and Background**: Briefly state the broader problem or knowledge gap this paper addresses.
2. **Objective**: Explain the primary goal or hypothesis.
3. **Methodology**: Summarize the methods, techniques, or data used by the authors.
4. **Key Findings**: Highlight the main results or claims.
5. **Implications and Future Work**: Discuss why these findings matter and suggest potential directions for further research.
6. **Critique**: Give the flaws and limitations of the paper.

Your summary should be clear, well-organized, and free from personal opinions or additional interpretations that are not supported by the text. Output format uses Markdown, paying attention to indentation (4 spaces).`;

// Prompt for "map-reduce" method, which is used when there are multiple splits
let map_prompt = `You are given a portion of a research paper "{title}" (a "chunk" of text). Your task is to produce a succinct and accurate summary of this chunk. Focus on the core ideas, methods, and any critical results or takeaways presented in this section.

Chunk content:
--- START OF CHUNK ---
{text}
--- END OF CHUNK ---

Structure your response using the following points:

1. **Context and Background**: Briefly state the broader problem or knowledge gap this paper addresses.
2. **Objective**: Explain the primary goal or hypothesis.
3. **Methodology**: Summarize the methods, techniques, or data used by the authors.
4. **Key Findings**: Highlight the main results or claims.
5. **Implications and Future Work**: Discuss why these findings matter and suggest potential directions for further research.
6. **Critique**: Give the flaws and limitations of the paper.

Guidelines:
- Be concise and neutral in tone.
- Do not include information outside of what appears in the chunk.
- If certain details (e.g., references or statistics) are not explicitly stated in the chunk, do not infer or fabricate them.
- Use bullet points or short paragraphs as appropriate.
- Output format uses standard Markdown, paying attention to indentation (4 spaces).`;

// Prompt for "reduce" in "map-reduce" method
let reduce_prompt = `You are given multiple summaries of research paper "{title}", each corresponding to a different chunk of a larger text. Your task is to synthesize these summaries into a single, cohesive overview of the entire paper.

Individual chunk summaries:
--- START OF CHUNK SUMMARIES ---
{text}
--- END OF CHUNK SUMMARIES ---

Please combine these partial summaries into one organized summary using the following points:

1. **Context and Background**: Briefly state the broader problem or knowledge gap this paper addresses.
2. **Objective**: Explain the primary goal or hypothesis.
3. **Methodology**: Summarize the methods, techniques, or data used by the authors.
4. **Key Findings**: Highlight the main results or claims.
5. **Implications and Future Work**: Discuss why these findings matter and suggest potential directions for further research.
6. **Critique**: Give the flaws and limitations of the paper.

Guidelines:
- Avoid duplicating information found in multiple chunk summaries.
- Use neutral, concise language.
- Do not invent new details not found in the chunk summaries.
- Output format uses standard Markdown, paying attention to indentation (4 spaces).`;
/************* Configurations End *************/


function formatString(str, params) {
    return str.replace(/{([^{}]*)}/g, (match, key) => {
        return params[key] || match;
    });
}

function check_attachment(attachment) {
    return attachment && (!only_link_file ||
        attachment.attachmentLinkMode === Zotero.Attachments.LINK_MODE_LINKED_FILE);
}

if (!item) return;

let progressWindow = undefined;
let itemProgress = undefined;
const window = require('window');

if (openaiBaseUrl.endsWith('/')) {
    openaiBaseUrl = openaiBaseUrl.slice(0, -1);
}

try {
    if (!item.isRegularItem() || !item.isTopLevelItem()) {
        return;
    }

    let title = item.getField('title');
    let link = item.getField('url') || "";

    const shortTitle = title.length > 50 ? title.substring(0, 50) + "..." : title;
    progressWindow = new Zotero.ProgressWindow({
        "closeOnClick": false,
    });
    progressWindow.addDescription(shortTitle);
    itemProgress = new progressWindow.ItemProgress();
    itemProgress.setItemTypeAndIcon("note");
    itemProgress.setText("Retrieving PDF...");
    progressWindow.show();

    // Check if the summary already exists
    let noteIds = item.getNotes();
    let summary_exist = false;
    for (const id of noteIds) {
        let note = Zotero.Items.get(id);
        let content = note.getNote();
        let header = headerTemplate.replace("{{modelName}}", modelName);
        if (content.startsWith(header)) {
            summary_exist = true;
            break;
        }
    }
    if (summary_exist) {
        itemProgress.setProgress(100);
        itemProgress.setText("Summary already exists.");
        progressWindow.startCloseTimer(5000);
        return;
    }

    // Get PDF attachment
    let pdfAttachment = await item.getBestAttachment();
    if (!check_attachment(pdfAttachment)) {
        let i = 0;
        while (i < timeout && (!check_attachment(pdfAttachment))) {
            await new Promise(r => setTimeout(r, 1000));
            pdfAttachment = await item.getBestAttachment();
            i++;
        }
    }
    if (!pdfAttachment) {
        throw new Error("No PDF attachment found for the selected item.");
    }

    let pdfPath = await pdfAttachment.getFilePath();
    const basePath = pdfPath.replace(/^.*[\\/]/, "");

    // Read PDF
    let fileData = await IOUtils.read(pdfPath);
    if (fileData instanceof ArrayBuffer) {
        fileData = new Uint8Array(fileData);
    }

    itemProgress.setProgress(20);
    itemProgress.setText("Parsing PDF...");

    // Step 1: Parse PDF
    const formData = new window.FormData();
    formData.append('title', title);
    formData.append('link', link);
    formData.append('chunk_size', chunkSize);
    formData.append('chunk_overlap', chunkOverlap);
    formData.append('pdf', new Blob([fileData], { type: 'application/pdf' }), basePath);

    const parseResponse = await fetch(`${serverUrl}/parse_pdf`, {
        method: "POST",
        body: formData
    });
    if (!parseResponse.ok) {
        let message = undefined;
        try {
            const data = await parseResponse.json();
            message = data.detail || data.error?.message;
        } catch (error) {}
        throw new Error(`${serverUrl} HTTP Error: ${parseResponse.status} ${parseResponse.statusText}${message ? ` - ${message}` : ''}`);
    }
    let parseResult, splits;
    try {
        parseResult = await parseResponse.json();
        splits = parseResult.splits;
    } catch (error) {
        throw new Error(`Error when parsing json of ${serverUrl}/parse_pdf: ${error.message}`);
    }

    // Step 2: Generate summary
    itemProgress.setProgress(40);
    itemProgress.setText("Generating summary...");
    const markdownSummary = await summarizeText(title, splits);

    // Step 3: Convert to HTML
    itemProgress.setProgress(80);
    itemProgress.setText("Formatting summary...");
    const htmlFormData = new window.FormData();
    htmlFormData.append('title', title);
    htmlFormData.append('markdown', markdownSummary);
    htmlFormData.append('model_name', modelName);
    htmlFormData.append('header_template', headerTemplate);

    const htmlResponse = await fetch(`${serverUrl}/md_to_html`, {
        method: "POST",
        body: htmlFormData
    });
    if (!htmlResponse.ok) {
        let message = undefined;
        try {
            const data = await htmlResponse.json();
            message = data.detail || data.error?.message;
        } catch (error) {}
        throw new Error(`${serverUrl} HTTP Error: ${htmlResponse.status} ${htmlResponse.statusText}${message ? ` - ${message}` : ''}`);
    }
    let htmlResult;
    try {
        htmlResult = await htmlResponse.json();
    } catch (error) {
        throw new Error(`Error when parsing json of ${serverUrl}/md_to_html: ${error.message}`);
    }

    // Create note with HTML content
    let newNote = new Zotero.Item('note');
    newNote.setNote(htmlResult.html);
    newNote.parentID = item.id;
    newNote.libraryID = item.libraryID;
    await newNote.saveTx();

    itemProgress.setProgress(100);
    itemProgress.setText("Summary generated successfully!");
    progressWindow.startCloseTimer(5000);
} catch (error) {
    itemProgress.setError();
    itemProgress.setText(`Error processing item: ${error.message}`);
    progressWindow.addDescription("");
    progressWindow.startCloseTimer(5000);
}

async function openaiRequest(message) {
    const response = await fetch(`${openaiBaseUrl}/chat/completions`, {
        method: 'POST',
        headers: {
            'Authorization': `Bearer ${apiKey}`,
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            model: modelName,
            messages: [{
                role: 'user',
                content: message
            }],
            temperature: 0.3
        })
    });
    if (!response.ok) {
        let message = undefined;
        try {
            const data = await response.json();
            message = data.detail || data.error?.message;
        } catch (error) {}
        throw new Error(`${openaiBaseUrl} HTTP Error: ${response.status} ${response.statusText}${message ? ` - ${message}` : ''}`);
    }

    let result;
    try {
        result = await response.json();
    } catch (error) {
        throw new Error(`Error when parsing json of ${openaiBaseUrl}/chat/completions: ${error.message}`);
    }
    if (!result.choices) {
        throw new Error("LLM API call failed!");
    }
    return result.choices[0].message.content;
}

async function summarizeText(title, splits) {
    // If only one split, use "stuff" method
    if (splits.length === 1) {
        const response = await openaiRequest(formatString(stuff_prompt, { title: title, text: splits[0].content }));
        return response;
    }

    // For multiple splits, use map-reduce method
    const summaries = await Promise.all(splits.map(async split => {
        const response = await openaiRequest(formatString(map_prompt, { title: title, text: split.content }));
        return response;
    }));
    itemProgress.setProgress(60);
    const combinedSummary = summaries.join('\n\n');
    const response = await openaiRequest(formatString(reduce_prompt, { title: title, text: combinedSummary }));
    return response;
}
