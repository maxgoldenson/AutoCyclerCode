import tkinter as tk

class KeyboardApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Keyboard Controlled UI")
        self.root.geometry("400x300")
        self.root.configure(bg="#1e1e2e")  # Dark theme background

        # App heading
        self.title_label = tk.Label(
            root, 
            text="Press Arrow Keys, Space, or Esc", 
            font=("Arial", 14, "bold"), 
            bg="#1e1e2e", 
            fg="#cdd6f4"
        )
        self.title_label.pack(pady=20)

        # Main interactive status box
        self.status_box = tk.Label(
            root, 
            text="READY", 
            font=("Arial", 18, "bold"), 
            bg="#313244", 
            fg="#a6e3a1", 
            width=15, 
            height=3
        )
        self.status_box.pack(pady=20)

        # Instructions helper footer
        self.footer = tk.Label(
            root, 
            text="Esc = Close App", 
            font=("Arial", 10), 
            bg="#1e1e2e", 
            fg="#6c7086"
        )
        self.footer.pack(side="bottom", pady=10)

        # Bind keyboard events to the main window
        self.root.bind("<KeyPress>", self.on_key_press)

    def on_key_press(self, event):
        # Retrieve the string name of the key pressed
        key = event.keysym  
        
        # Route keys to specific UI changes
        if key == "Up":
            self.update_ui("MOVING UP", "#89b4fa")
        elif key == "Down":
            self.update_ui("MOVING DOWN", "#f38ba8")
        elif key == "Left":
            self.update_ui("MOVING LEFT", "#fab387")
        elif key == "Right":
            self.update_ui("MOVING RIGHT", "#f9e2af")
        elif key == "space":
            self.update_ui("ACTION TRIGERRED", "#cba6f7")
        elif key == "Escape":
            self.root.destroy()  # Gracefully close the window

    def update_ui(self, text, color):
        """Helper to quickly change text and styling on keypress"""
        self.status_box.config(text=text, fg=color)


# Run the application
if __name__ == "__main__":
    root = tk.Tk()
    app = KeyboardApp(root)
    root.mainloop()

