// server.js
// Express 伺服器，處理音檔與影片轉文字，使用 OpenAI Whisper API

const express = require('express');
const multer = require('multer');
const fs = require('fs');
const cors = require('cors');
const { Configuration, OpenAIApi } = require('openai');

const app = express();
app.use(cors());

// 設定上傳資料夾與 multer
const upload = multer({ dest: 'uploads/' });

// 初始化 OpenAI 客戶端
const configuration = new Configuration({
  apiKey: process.env.OPENAI_API_KEY,
});
const openai = new OpenAIApi(configuration);

// 封裝轉錄函式
async function transcribe(filePath) {
  const response = await openai.createTranscription(
    fs.createReadStream(filePath),
    'whisper-1'
  );
  return response.data.text;
}

// 音檔轉錄路由
app.post('/api/transcribe-audio', upload.single('file'), async (req, res) => {
  try {
    const transcript = await transcribe(req.file.path);
    res.json({ transcript });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: err.message });
  } finally {
    // 刪除暫存檔
    fs.unlink(req.file.path, () => {});
  }
});

// 影片轉錄路由
app.post('/api/transcribe-video', upload.single('file'), async (req, res) => {
  try {
    const transcript = await transcribe(req.file.path);
    res.json({ transcript });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: err.message });
  } finally {
    fs.unlink(req.file.path, () => {});
  }
});

// 啟動伺服器
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Server listening on port ${PORT}`);
});

/**
 * 安裝指令:
 * npm install express multer cors openai
 * 
 * 執行:
 * export OPENAI_API_KEY="你的API金鑰"
 * node server.js
 */
