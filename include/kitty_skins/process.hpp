#pragma once

#include <span>
#include <string>
#include <sys/types.h>
#include <vector>

namespace kitty_skins {

struct ProcessResult { int exitCode; std::string standardError; };
ProcessResult run(std::span<const std::string> argv);

// The caller's compositor identity from HYPRLAND_INSTANCE_SIGNATURE. Empty when
// the caller runs outside a known compositor session, in which case no Kitty
// process may ever be signaled.
std::string ownCompositorSignature();

// Kitty processes of the calling UID joined to the given compositor instance
// only. An empty signature returns nothing: there is deliberately no fallback
// that would enumerate every Kitty on the machine.
std::vector<pid_t> findKittyProcesses(const std::string& compositorSignature);
void signalKittyProcesses(std::span<const pid_t> pids);

}
