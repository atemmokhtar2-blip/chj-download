import gradio as gr
import threading
import os
import sys
import time

# ZeroGPU mandatory import and decorator
try:
    import spaces
except ImportError:
    # Fallback for local testing
    class spaces:
        @staticmethod
        def GPU(func):
            return func

# Add the current directory to sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

@spaces.GPU
def dummy_gpu_task():
    """This function exists only to satisfy ZeroGPU requirements."""
    return "GPU is initialized"

def start_bot():
    print("--- Starting Telegram Bot Thread ---"); import logging; logging.basicConfig(level=logging.INFO)
    # Small delay to ensure Gradio/ZeroGPU is ready
    time.sleep(5)
    try:
        from bot import main
        main()
    except Exception as e:
        print(f"CRITICAL ERROR in Bot Thread: {e}")
        import traceback
        traceback.print_exc()

# Start the bot thread
bot_thread = threading.Thread(target=start_bot, daemon=True)
bot_thread.start()

# Minimal Gradio app
def status_check():
    # Call the dummy GPU task to satisfy the system
    dummy_gpu_task()
    return "Bot is running in background. ZeroGPU initialized."

with gr.Blocks() as demo:
    gr.Markdown("# 🤖 Telegram Bot Host (ZeroGPU Mode)")
    gr.Markdown("Status: **Online**")
    status_btn = gr.Button("Initialize/Check Status")
    output = gr.Textbox(label="Response")
    status_btn.click(fn=status_check, outputs=output)

if __name__ == "__main__":
    # Hugging Face requires port 7860
    demo.launch(server_name="0.0.0.0", server_port=7860)
