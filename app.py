import gradio as gr
import threading
import os
import sys
import time

# Add the current directory to sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

def start_bot():
    print("--- Starting Telegram Bot Thread ---")
    try:
        from bot import main
        main()
    except Exception as e:
        print(f"CRITICAL ERROR in Bot Thread: {e}")
        import traceback
        traceback.print_exc()

# Start the bot thread immediately
bot_thread = threading.Thread(target=start_bot, daemon=True)
bot_thread.start()

# Minimal Gradio app
def status_check():
    return "Bot is running in background thread."

with gr.Blocks() as demo:
    gr.Markdown("# 🤖 Telegram Bot Host")
    gr.Markdown("Status: **Online**")
    status_btn = gr.Button("Check Status")
    output = gr.Textbox(label="Response")
    status_btn.click(fn=status_check, outputs=output)

if __name__ == "__main__":
    # Hugging Face requires port 7860
    demo.launch(server_name="0.0.0.0", server_port=7860)
