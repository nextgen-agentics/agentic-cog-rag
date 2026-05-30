/* ==========================================================================
   Agent Control Hub — Frontend Core Logic
   ========================================================================== */

document.addEventListener('DOMContentLoaded', () => {
    initTabs();
    initGatewayStatus();
    initForms();
});

// ==========================================================================
// Tab Navigation
// ==========================================================================
function initTabs() {
    const buttons = document.querySelectorAll('.tab-btn');
    const contents = document.querySelectorAll('.tab-content');

    buttons.forEach(btn => {
        btn.addEventListener('click', () => {
            const tabId = btn.getAttribute('data-tab');

            // Set active button
            buttons.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');

            // Set active panel
            contents.forEach(c => {
                c.classList.remove('active');
                if (c.getAttribute('id') === tabId) {
                    c.classList.add('active');
                }
            });
        });
    });
}

// ==========================================================================
// Gateway Status Monitoring
// ==========================================================================
function initGatewayStatus() {
    const badge = document.getElementById('gateway-status');
    const badgeText = badge.querySelector('.badge-text');

    async function checkStatus() {
        try {
            const res = await fetch('/api/status');
            const data = await res.json();

            if (data.gateway === 'online') {
                badge.className = 'status-badge online';
                badgeText.textContent = 'ONLINE (Port 8107)';
            } else {
                badge.className = 'status-badge offline';
                badgeText.textContent = 'OFFLINE';
            }
        } catch (e) {
            badge.className = 'status-badge offline';
            badgeText.textContent = 'OFFLINE';
        }
    }

    // Ping status immediately, then every 5 seconds
    checkStatus();
    setInterval(checkStatus, 5000);
}

// ==========================================================================
// Form Submissions & Streaming
// ==========================================================================
// ==========================================================================
// Form Submissions & Streaming
// ==========================================================================
async function stopTask(taskType, controller, logElement, stopBtn) {
    if (controller) {
        controller.abort();
    }
    
    if (stopBtn) {
        stopBtn.classList.add('hidden');
    }
    
    if (logElement) {
        logElement.innerHTML += `<span class="log-warn">\n[STOPPING] Sending stop signal to server for task: ${taskType}...</span>\n`;
        logElement.scrollTop = logElement.scrollHeight;
    }
    
    try {
        const res = await fetch('/api/stop', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ task_type: taskType })
        });
        const data = await res.json();
        if (logElement) {
            logElement.innerHTML += `<span class="log-error">[STOPPED] ${data.message}</span>\n`;
            logElement.scrollTop = logElement.scrollHeight;
        }
    } catch (err) {
        if (logElement) {
            logElement.innerHTML += `<span class="log-error">[ERROR] Failed to send stop command: ${err.message}</span>\n`;
            logElement.scrollTop = logElement.scrollHeight;
        }
    }
}

function initForms() {
    // 1. PDF Conversion Form
    const convertForm = document.getElementById('convert-form');
    const convertLog = document.getElementById('convert-log');
    const convertBtn = document.getElementById('convert-btn');
    const convertStopBtn = document.getElementById('convert-stop-btn');
    let convertController = null;

    if (convertStopBtn) {
        convertStopBtn.addEventListener('click', () => {
            stopTask('convert', convertController, convertLog, convertStopBtn);
        });
    }

    convertForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        
        const inputDir = document.getElementById('convert-input').value.trim();
        const outputDir = document.getElementById('convert-output').value.trim();
        const overwrite = document.getElementById('convert-overwrite').checked;

        convertLog.textContent = 'Resolving folders and initializing Microsoft MarkItDown converter...\n';
        convertBtn.classList.add('loading');
        convertBtn.disabled = true;
        if (convertStopBtn) convertStopBtn.classList.remove('hidden');

        convertController = new AbortController();

        try {
            const response = await fetch('/api/convert', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    input_dir: inputDir,
                    output_dir: outputDir || null,
                    overwrite: overwrite
                }),
                signal: convertController.signal
            });

            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || 'Failed to start conversion');
            }

            await readStream(response, convertLog, (line) => colorizeLine(line));
        } catch (err) {
            if (err.name === 'AbortError') {
                // Handled
            } else {
                convertLog.innerHTML += `<span class="log-error">\n[ERROR] ${err.message}</span>\n`;
            }
        } finally {
            convertBtn.classList.remove('loading');
            convertBtn.disabled = false;
            if (convertStopBtn) convertStopBtn.classList.add('hidden');
            convertController = null;
        }
    });

    // 2. Indexer Form
    const indexForm = document.getElementById('index-form');
    const indexLog = document.getElementById('index-log');
    const indexBtn = document.getElementById('index-btn');
    const indexStopBtn = document.getElementById('index-stop-btn');
    let indexController = null;

    if (indexStopBtn) {
        indexStopBtn.addEventListener('click', () => {
            stopTask('index', indexController, indexLog, indexStopBtn);
        });
    }

    indexForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        
        const inputDir = document.getElementById('index-input').value.trim();

        indexLog.textContent = 'Resolving paths and verifying sandbox security boundary...\n';
        indexBtn.classList.add('loading');
        indexBtn.disabled = true;
        if (indexStopBtn) indexStopBtn.classList.remove('hidden');

        indexController = new AbortController();

        try {
            const response = await fetch('/api/index', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    input_dir: inputDir
                }),
                signal: indexController.signal
            });

            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || 'Failed to start indexing');
            }

            await readStream(response, indexLog, (line) => colorizeLine(line));
        } catch (err) {
            if (err.name === 'AbortError') {
                // Handled
            } else {
                indexLog.innerHTML += `<span class="log-error">\n[ERROR] ${err.message}</span>\n`;
            }
        } finally {
            indexBtn.classList.remove('loading');
            indexBtn.disabled = false;
            if (indexStopBtn) indexStopBtn.classList.add('hidden');
            indexController = null;
        }
    });

    // 3. Agent Execution Form
    const agentForm = document.getElementById('agent-form');
    const agentLog = document.getElementById('agent-log');
    const agentBtn = document.getElementById('agent-btn');
    const agentStopBtn = document.getElementById('agent-stop-btn');
    const answerPanel = document.getElementById('answer-panel');
    const agentAnswer = document.getElementById('agent-answer');
    const traceContainer = document.getElementById('agent-trace-container');
    let agentController = null;

    if (agentStopBtn) {
        agentStopBtn.addEventListener('click', () => {
            stopTask('agent', agentController, agentLog, agentStopBtn);
        });
    }

    agentForm.addEventListener('submit', async (e) => {
        e.preventDefault();

        const query = document.getElementById('agent-query').value.trim();

        // Clear and reset UI
        agentLog.textContent = 'Starting process and establishing stdio MCP tool bindings...\n';
        answerPanel.classList.add('hidden');
        agentAnswer.textContent = '';
        traceContainer.innerHTML = ''; 
        traceContainer.appendChild(agentLog); // Append the raw terminal to trace-container initially

        agentBtn.classList.add('loading');
        agentBtn.disabled = true;
        if (agentStopBtn) agentStopBtn.classList.remove('hidden');

        // Tracing parser state
        let currentTraceStep = null;
        let finalAnswerAccumulator = "";
        let isFinalAnswerRecording = false;

        agentController = new AbortController();

        try {
            const response = await fetch('/api/agent', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ query: query }),
                signal: agentController.signal
            });

            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || 'Failed to trigger agent');
            }

            await readStream(response, agentLog, (line) => {
                const colored = colorizeLine(line);
                parseAndAddTrace(line);
                return colored;
            });

        } catch (err) {
            if (err.name === 'AbortError') {
                // Handled
            } else {
                agentLog.innerHTML += `<span class="log-error">\n[ERROR] ${err.message}</span>\n`;
            }
        } finally {
            agentBtn.classList.remove('loading');
            agentBtn.disabled = false;
            if (agentStopBtn) agentStopBtn.classList.add('hidden');
            agentController = null;
        }

        // --- Parsing Traces Helper ---
        function parseAndAddTrace(line) {
            const trimmed = line.trim();
            if (!trimmed) return;

            // 1. Check for Iteration headers
            if (trimmed.startsWith('─── iter')) {
                const iterMatch = trimmed.match(/iter\s+(\d+)/i);
                const iterNum = iterMatch ? iterMatch[1] : '';
                createTraceCard('perception', `🔄 Cognitive Iteration ${iterNum}`, `Beginning execution loop iteration ${iterNum}...`);
                return;
            }

            // 2. Check for Memory Read
            if (trimmed.startsWith('[memory.read]')) {
                createTraceCard('memory', '📥 Memory Service Read', trimmed);
                return;
            }

            // 3. Check for Perception Goals
            if (trimmed.startsWith('[perception]')) {
                if (!currentTraceStep || currentTraceStep.type !== 'perception') {
                    createTraceCard('perception', '👁 Perception Goal Layer', trimmed);
                } else {
                    appendToTraceCard(trimmed);
                }
                return;
            }

            // 4. Check for Decision making (answer or tool call)
            if (trimmed.startsWith('[decision]')) {
                let title = '🎯 Decision Engine Layer';
                let content = trimmed;
                if (trimmed.includes('ANSWER:')) {
                    title = '🎯 Decision: Formulate Answer';
                } else if (trimmed.includes('TOOL_CALL:')) {
                    title = '🎯 Decision: Execute Action Tool';
                }
                createTraceCard('decision', title, content);
                return;
            }

            // 5. Check for Action tool execution outcomes
            if (trimmed.startsWith('[action]')) {
                createTraceCard('action', '🔧 Action Execution outcomes', trimmed);
                return;
            }

            // 6. Check for final answer printing
            if (trimmed.startsWith('FINAL:')) {
                isFinalAnswerRecording = true;
                const answerText = trimmed.substring(6).trim();
                finalAnswerAccumulator = answerText;
                displayFinalAnswer(finalAnswerAccumulator);
                return;
            }

            // If recording final answer blocks (handles trailing lines or multiple line responses)
            if (isFinalAnswerRecording && !trimmed.startsWith('═')) {
                // If it hits the final bounds marker
                if (trimmed.includes('═════════════')) {
                    isFinalAnswerRecording = false;
                } else {
                    finalAnswerAccumulator += "\n" + trimmed;
                    displayFinalAnswer(finalAnswerAccumulator);
                }
            }
        }

        function createTraceCard(type, title, description) {
            // Remove agent raw logs if they are still alone
            if (traceContainer.contains(agentLog)) {
                traceContainer.removeChild(agentLog);
            }

            const step = document.createElement('div');
            step.className = `trace-step ${type}`;
            
            const meta = document.createElement('div');
            meta.className = 'step-meta';
            
            const label = document.createElement('span');
            label.className = 'label';
            label.innerHTML = title;
            
            const time = document.createElement('span');
            time.className = 'time';
            time.textContent = new Date().toLocaleTimeString();

            meta.appendChild(label);
            meta.appendChild(time);

            const desc = document.createElement('div');
            desc.className = 'step-desc';
            desc.textContent = description;

            step.appendChild(meta);
            step.appendChild(desc);
            
            traceContainer.appendChild(step);
            traceContainer.scrollTop = traceContainer.scrollHeight;

            currentTraceStep = { type: type, element: desc };
        }

        function appendToTraceCard(text) {
            if (currentTraceStep && currentTraceStep.element) {
                currentTraceStep.element.textContent += '\n' + text;
                traceContainer.scrollTop = traceContainer.scrollHeight;
            }
        }

        function displayFinalAnswer(text) {
            answerPanel.classList.remove('hidden');
            
            if (window.marked && window.marked.parse) {
                // Compile standard Github Flavored Markdown to HTML
                agentAnswer.innerHTML = window.marked.parse(text);
            } else {
                // Robust native fallback
                let formatted = text
                    .replace(/&/g, "&amp;")
                    .replace(/</g, "&lt;")
                    .replace(/>/g, "&gt;")
                    .replace(/`([^`]+)`/g, '<code>$1</code>')
                    .replace(/\n\n/g, '</p><p>')
                    .replace(/\n/g, '<br>');
                agentAnswer.innerHTML = `<p>${formatted}</p>`;
            }
        }
    });
}

// ==========================================================================
// Stream Reader Helper
// ==========================================================================
async function readStream(response, logElement, lineModifier) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    logElement.textContent = ''; // clear waiting placeholder

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');

        // Keep the last partial line in buffer
        buffer = lines.pop();

        for (const line of lines) {
            const modified = lineModifier(line);
            logElement.innerHTML += modified + '\n';
            logElement.scrollTop = logElement.scrollHeight;
        }
    }

    // Flush remaining buffer
    if (buffer) {
        const modified = lineModifier(buffer);
        logElement.innerHTML += modified;
        logElement.scrollTop = logElement.scrollHeight;
    }
}

// ==========================================================================
// CSS Styling / Syntax coloring rules for log streams
// ==========================================================================
function colorizeLine(line) {
    const safeLine = line
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");

    // Check levels
    if (safeLine.includes('| ERROR   |') || safeLine.startsWith('[ERROR]') || safeLine.includes('✗ Failed')) {
        return `<span class="log-error">${safeLine}</span>`;
    }
    if (safeLine.includes('| WARNING |') || safeLine.startsWith('SKIP') || safeLine.startsWith('No PDF files found')) {
        return `<span class="log-warn">${safeLine}</span>`;
    }
    if (safeLine.includes('✓ Saved') || safeLine.includes('✓') || safeLine.includes('OK  ') || safeLine.includes('Summary')) {
        return `<span class="log-info">${safeLine}</span>`;
    }
    if (safeLine.startsWith('─── iter') || safeLine.startsWith('run ') || safeLine.startsWith('FINAL:')) {
        return `<span class="log-highlight">${safeLine}</span>`;
    }
    return safeLine;
}

// Clear log panels manually
function clearLog(elementId) {
    const log = document.getElementById(elementId);
    if (log) {
        log.textContent = 'Console logs cleared.';
    }
    
    // Custom full reset when clearing the Agent console (agent-log)
    if (elementId === 'agent-log') {
        const traceContainer = document.getElementById('agent-trace-container');
        const answerPanel = document.getElementById('answer-panel');
        const agentAnswer = document.getElementById('agent-answer');
        
        if (traceContainer) {
            traceContainer.innerHTML = '';
            
            // Re-create the clean pre block for future stdout streaming
            const newLog = document.createElement('pre');
            newLog.id = 'agent-log';
            newLog.className = 'terminal-log';
            newLog.textContent = 'Enter a query above to launch the agent cognitive loop...';
            traceContainer.appendChild(newLog);
        }
        
        if (answerPanel) {
            answerPanel.classList.add('hidden');
        }
        if (agentAnswer) {
            agentAnswer.textContent = '';
        }
        
        // Empty the textarea query input
        const queryInput = document.getElementById('agent-query');
        if (queryInput) {
            queryInput.value = '';
        }
    }
}
