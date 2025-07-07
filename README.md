# LLM Paper Summarizer for Zotero

在 Zotero 中使用 LLM 自动总结论文并生成笔记。

Automatically summarize academic papers and generate notes in Zotero using an LLM.

工作流程：以 [zotero-actions-tags](https://github.com/windingwind/zotero-actions-tags) 脚本的形式实现。在 zotero 中新增论文时，zotero-actions-tags 触发 JavaScript 脚本，获取论文的 PDF 文件地址和论文名，并向服务器发送 PDF 进行解析与分割。之后本地调用 LLM API 总结论文。获得总结后将生成的 markdown 发送给服务器转换为 html。最后将 html 总结写入到论文笔记中。

Workflow: this tool is implemented as a script for [zotero-actions-tags](https://github.com/windingwind/zotero-actions-tags). When a new paper is added to Zotero, the zotero-actions-tags plugin triggers a JavaScript script to retrieve the PDF file path and paper title. It then sends the PDF to a server for analysis and segmentation. Next, the local system calls the LLM API to generate a summary of the paper. Once the summary is obtained, the generated markdown is sent to the server to be converted into HTML. Finally, the HTML summary is written into the paper's note.

![example](https://qyzhang-obsidian.oss-cn-hangzhou.aliyuncs.com/20250124100826.png)

## 部署 | Setup

安装 [zotero-actions-tags](https://github.com/windingwind/zotero-actions-tags) 插件，并按照下图配置。

Install the [zotero-actions-tags](https://github.com/windingwind/zotero-actions-tags) plugin and configure it as shown below:

![zotero-actions-tags settings](https://qyzhang-obsidian.oss-cn-hangzhou.aliyuncs.com/20250124094839.png)

![edit action](https://qyzhang-obsidian.oss-cn-hangzhou.aliyuncs.com/20250124095407.png)

## 配置 | Configuration

关键点：如果使用 [ZotMoov](https://github.com/wileyyugioh/zotmoov) 或 [ZotFile](https://github.com/jlegewie/zotfile) 把论文保存在 OneDrive 等同步盘上，将 `only_link_file` 设为 `true`；根据想要使用的大模型 API 配置 `openaiBaseUrl`、`modelName` 和 `apiKey`；将 `chunkSize` 调整为模型上下文长度；将 `stuffPrompt`、`mapPrompt` 和 `reducePrompt` 翻译成你的语言，注意保留其中的 `{title}` 和 `{text}` 不变。

TLDR: If you use [ZotMoov](https://github.com/wileyyugioh/zotmoov) or [ZotFile](https://github.com/jlegewie/zotfile) to save your papers on OneDrive or other synchronized drives, set `only_link_file` to `true`; configure `openaiBaseUrl`, `modelName`, and `apiKey` according to the LLM API you intend to use; adjust `chunkSize` to match the model's context length; translate `stuffPrompt`, `mapPrompt`, and `reducePrompt` into your preferred language, ensuring that the placeholders `{title}` and `{text}` remain unchanged.

在 `zotero_script.js` 代码的顶端包含了一些配置，如下所示。

At the beginning of the `zotero_script.js` file, you’ll find some configurable options:

```js
let serverUrl = "https://paper_summarizer.jianyue.tech";
let only_link_file = false;
let timeout = 30;
let openaiBaseUrl = "https://dashscope.aliyuncs.com/compatible-mode/v1";
// Gemini
// let openaiBaseUrl = "https://generativelanguage.googleapis.com/v1beta/openai/";
let modelName = "qwen-plus-latest";
let apiKey = "sk-xxxxxxxxxxxxx";
let chunkSize = 64000;
let chunkOverlap = 1000;
let stuffPrompt = "";
let mapPrompt = "";
let reducePrompt = "";
```

- **`serverUrl`**：用于解析 PDF 文件和将总结后的 markdown 转换为 html 的服务器地址。默认为作者公开的服务器。**如果你需要对敏感文件总结，推荐自己部署**，只需 clone 本仓库后执行 `python server.py` 即可。
- **`only_link_file`**：配合 [ZotMoov](https://github.com/wileyyugioh/zotmoov) 或 [ZotFile](https://github.com/jlegewie/zotfile) 使用。如果使用这两个或类似的插件将论文 PDF 以 Zotero 的 "Link to File" 形式保存，则应设置 `only_link_file` 为 `true`，否则设置为 `false`。
- **`timeout`**：配合 [ZotMoov](https://github.com/wileyyugioh/zotmoov) 或 [ZotFile](https://github.com/jlegewie/zotfile) 使用。在新增论文时，最多等待多少秒后检查 PDF 是否下载完成。
- **`openaiBaseUrl`**：OpenAI 兼容的 api 地址。具体的地址取决于使用的模型提供商，默认是通义千问，详见 <https://www.aliyun.com/product/bailian>。
- **`modelName`**：调用模型 API 时提供的模型名。
- **`apiKey`**：LLM 的 api key。
- **`chunkSize`**：模型的上下文大小。超过上下文的 PDF 文档需要拆分成多个分片，使用 map-reduce 方案总结。
- **`chunkOverlap`**：map-reduce 方案下分片间重合的大小。
- **`stuffPrompt`**：当 PDF 文档没有超出模型上下文时使用的提示词。
- **`mapPrompt`**：map-reduce 方案下 map 阶段使用的提示词。
- **`reducePrompt`**：map-reduce 方案下 reduce 阶段使用的提示词。

<br>

- **`serverUrl`**: The server URL for parsing PDF files and converting the summarized markdown into HTML. Defaults to the author's public server. **If you need to summarize sensitive files, it's recommended to deploy your own server**; simply clone this repository and run `python server.py`.
- **`only_link_file`**: Use this in conjunction with [ZotMoov](https://github.com/wileyyugioh/zotmoov) or [ZotFile](https://github.com/jlegewie/zotfile). If you save PDF papers in Zotero as "Link to File" using these (or similar) plugins, set `only_link_file` to `true`; otherwise, set it to `false`.
- **`timeout`**: Also used with [ZotMoov](https://github.com/wileyyugioh/zotmoov) or [ZotFile](https://github.com/jlegewie/zotfile). This parameter specifies the maximum number of seconds to wait after adding a new paper before checking whether the PDF download is complete.
- **`openaiBaseUrl`**: The API endpoint compatible with OpenAI. The specific URL depends on the model provider you are using; by default, it is Qwen, see <https://www.aliyun.com/product/bailian>.
- **`modelName`**: The model name provided when calling the API.
- **`apiKey`**: The API key for the LLM.
- **`chunkSize`**: The model's context size. PDF documents that exceed the context window must be split into multiple segments and summarized using a map-reduce approach.
- **`chunkOverlap`**: The overlap size between segments when using the map-reduce approach.
- **`stuffPrompt`**: The prompt used when the entire PDF document fits within the model’s context window.
- **`mapPrompt`**: The prompt used during the map phase of the map-reduce approach.
- **`reducePrompt`**: The prompt used during the reduce phase of the map-reduce approach.
