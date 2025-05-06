// chat_server.js
// Express server for AI-powered chat using OpenAI Chat Completions API with session history

const express = require('express');
const cors = require('cors');
const session = require('express-session');
const { Configuration, OpenAIApi } = require('openai');

require('dotenv').config(); // load OPENAI_API_KEY from .env

const app = express();
app.use(cors({ origin: true, credentials: true }));
app.use(express.json());
app.use(session({
  secret: process.env.SESSION_SECRET || 'change_this_secret',
  resave: false,
  saveUninitialized: true,
  cookie: { secure: false }
}));

const configuration = new Configuration({ apiKey: process.env.OPENAI_API_KEY });
const openai = new OpenAIApi(configuration);

// Initialize system prompt
const systemPrompt = { role: 'system', content: '你是一個智能問答助理，回答應該清晰、有邏輯並友善。' };

app.post('/api/chat', async (req, res) => {
  try {
    const { question } = req.body;
    // Initialize conversation history in session
    if (!req.session.messages) {
      req.session.messages = [systemPrompt];
    }
    // Append user message
    req.session.messages.push({ role: 'user', content: question });

    // Call OpenAI chat completion with full history
    const completion = await openai.createChatCompletion({
      model: 'gpt-4',  // upgrade to GPT-4 for smarter responses
      messages: req.session.messages,
      temperature: 0.7
    });

    const answer = completion.data.choices[0].message.content.trim();
    // Append assistant response to history
    req.session.messages.push({ role: 'assistant', content: answer });

    res.json({ answer });
  } catch (error) {
    console.error('Chat API error:', error.message);
    res.status(500).json({ answer: '抱歉，無法處理您的請求。' });
  }
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Chat server listening on port ${PORT}`);
});

/**
 * 
 * 1. 在專案根目錄建立 .env:
 *    OPENAI_API_KEY=你的金鑰
 *    SESSION_SECRET=自行設定
 * 
 * 2. 安裝相依套件:
 *    npm install express cors express-session openai dotenv
 *
 * 3. 啟動服務:
 * node chat_server.js
 * 
 * 就能讓前後端完整串接，實現智能問答功能。
 */
