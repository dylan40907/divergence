# Headless Godot fixture: CPU diagnostic frames, not GPU viewport screenshots.
extends SceneTree

const Door = preload("res://door.gd")

func _initialize() -> void:
	var config_path := OS.get_environment("DIVERGENCE_SCENARIO")
	var output := OS.get_environment("DIVERGENCE_OUTPUT")
	var config: Dictionary = JSON.parse_string(FileAccess.get_file_as_string(config_path))
	DirAccess.make_dir_recursive_absolute(output.path_join("frames"))
	var door := Door.new()
	var player_x: float = 140.0
	var samples: Array = []
	for t in range(0, int(config.duration_ms) + 1, int(config.sample_ms)):
		var events: Array = []
		if t == 3000:
			door.activate()
			events.append("DoorOpened")
		if t >= 3500:
			var target: float = minf(760.0, player_x + float(config.sample_ms) * 0.11)
			player_x = minf(455.0, target) if door.collider_enabled else target
		var state := {"door": {"open": door.open, "collider_enabled": door.collider_enabled},
			"player": {"x": snappedf(player_x, 0.0001), "reached_exit": player_x >= 750.0}, "clock_ms": t}
		var frame: String = "frames/%05d.png" % samples.size()
		var image := Image.create(960, 540, false, Image.FORMAT_RGB8)
		image.fill(Color("0e1520"))
		image.fill_rect(Rect2i(38, 92, 884, 378), Color("182334"))
		for gx in range(58, 920, 32):
			image.fill_rect(Rect2i(gx, 110, 1, 340), Color("223246"))
		for gy in range(110, 450, 32):
			image.fill_rect(Rect2i(58, gy, 844, 1), Color("223246"))
		image.fill_rect(Rect2i(480, 94, 20, 121), Color("455670"))
		image.fill_rect(Rect2i(480, 335, 20, 132), Color("455670"))
		if not door.open:
			image.fill_rect(Rect2i(480, 215, 20, 120), Color("f0b66a"))
		else:
			image.fill_rect(Rect2i(480, 205, 20, 5), Color("70dfc0"))
			image.fill_rect(Rect2i(480, 340, 20, 5), Color("70dfc0"))
		image.fill_rect(Rect2i(270, 145, 43, 39), Color("70dfc0") if door.open else Color("465671"))
		image.fill_rect(Rect2i(745, 260, 30, 30), Color("54778a"))
		image.fill_rect(Rect2i(int(player_x) - 15, 260, 30, 30), Color("8f9bff"))
		var error := image.save_png(output.path_join(frame))
		if error != OK:
			push_error("Could not save diagnostic frame")
			quit(1)
			return
		samples.append({"t_ms": t, "state": state, "events": events, "logs": [], "metrics": {}, "frame": frame})
	var trace := {"schema_version": 1, "seed": int(config.seed), "samples": samples,
		"metadata": {"adapter": "godot-command", "engine": Engine.get_version_info().string,
		"note": "Headless protocol fixture. Frames are CPU diagnostic images, not GPU gameplay captures."}}
	var file := FileAccess.open(output.path_join("trace.json"), FileAccess.WRITE)
	if file == null:
		quit(1)
		return
	file.store_string(JSON.stringify(trace))
	file.close()
	quit(0)
