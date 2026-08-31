class Door:
    def __init__(self):
        self.open = False
        self.collider_enabled = True

    def activate(self):
        self.open = True
        self.collider_enabled = False
