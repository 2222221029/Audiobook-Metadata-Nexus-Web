# progress.py

class ProgressManager:
    def __init__(self, total_steps=100):
        self.total_steps = total_steps
        self.current_step = 0
        self.message = ""
    
    def update(self, step, message=""):
        self.current_step = step
        self.message = message
        progress = (step / self.total_steps) * 100
        return progress, message
    
    def increment(self, increment=1, message=""):
        self.current_step += increment
        self.message = message
        progress = (self.current_step / self.total_steps) * 100
        return progress, message
    
    def set_total(self, total_steps):
        self.total_steps = total_steps
        self.current_step = 0
    
    def get_progress(self):
        return (self.current_step / self.total_steps) * 100 if self.total_steps > 0 else 0
