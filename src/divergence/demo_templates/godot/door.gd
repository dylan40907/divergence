extends RefCounted

var open: bool = false
var collider_enabled: bool = true

func activate() -> void:
	open = true
	collider_enabled = false
