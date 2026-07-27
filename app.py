import gradio as gr
import threading
import os
import sys
import time
import logging
import asyncio

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("HF_APP")

# ZeroGPU mandatory import
try:
    import spaces
    logger.info("Successfully imported spaces")
except ImportError:
    class spaces:
        @staticmethod
        def GPU(func):
            return func
    logger.info("Spaces import failed, using fallback")

# Add current dir to path
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

@spaces.GPU
def dummy_gpu_task():
    return "GPU Initialized"

def start_bot():
    logger.info("--- STARTING BOT THREAD ---")
    time.sleep(2)
    
    # Use a persistent loop for the thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        logger.info("Importing main from bot...")
        from bot import main
        logger.info("Calling main()...")
        # Ensure the loop is kept alive and polling is handled correctly
        main()
    except Exception as e:
        logger.error(f"CRITICAL ERROR IN BOT THREAD: {e}", exc_info=True)
    finally:
        loop.close()

# Start bot thread
threading.Thread(target=start_bot, daemon=True).start()

# UI
with gr.Blocks() as demo:
    gr.Markdown("# 🤖 Bot Status Monitor")
    gr.Markdown("Status: **Active** (Check Telegram)")
    status_box = gr.Textbox(label="Last Action", value="Bot is starting...")
    refresh_btn = gr.Button("Refresh Status")
    
    def get_status():
        dummy_gpu_task()
        return f"System Time: {time.ctime()} | Bot Thread Active: {threading.active_count()}"
    
    refresh_btn.click(get_status, outputs=status_box)

if __name__ == "__main__":
    logger.info("Launching Gradio app...")
    demo.launch(server_name="0.0.0.0", server_port=7860)
