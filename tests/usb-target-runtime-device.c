/* A real exported PipeWire Device with no hardware or profile parameters.
 * Unlike a Lua policy mock, this traverses the native protocol and WP registry. */
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <pipewire/pipewire.h>
#include <pipewire/impl-module.h>
#include <spa/monitor/device.h>
#include <spa/utils/hook.h>

struct fixture {
  struct spa_device device;
  struct spa_hook_list listeners;
  struct pw_main_loop *loop;
  struct pw_core *core;
  struct pw_proxy *proxy;
  struct spa_hook proxy_listener;
  struct pw_properties *props;
};

static int add_listener(void *object, struct spa_hook *listener,
    const struct spa_device_events *events, void *data)
{
  struct fixture *f = object;
  spa_hook_list_append(&f->listeners, listener, events, data);
  struct spa_device_info info = {
    .version = SPA_VERSION_DEVICE_INFO,
    .change_mask = SPA_DEVICE_CHANGE_MASK_PROPS,
    .props = &f->props->dict,
  };
  if (events->info) events->info(data, &info);
  return 0;
}

static int sync_device(void *object, int seq)
{
  struct fixture *f = object;
  spa_hook_list_call(&f->listeners, struct spa_device_events, result,
      0, seq, 0, 0, NULL);
  return 0;
}

static const struct spa_device_methods methods = {
  SPA_VERSION_DEVICE_METHODS,
  .add_listener = add_listener,
  .sync = sync_device,
};

static void bound(void *data, uint32_t id)
{
  (void)data;
  printf("%u\n", id);
  fflush(stdout);
}

static const struct pw_proxy_events proxy_events = {
  PW_VERSION_PROXY_EVENTS, .bound = bound,
};

static void quit(void *data, int signal_number)
{
  (void)signal_number;
  pw_main_loop_quit(data);
}

int main(int argc, char **argv)
{
  pw_init(&argc, &argv);
  struct fixture f = {0};
  f.props = pw_properties_new("device.name", argc > 1 ? argv[1] : "fixture.usb",
      "device.api", "alsa", "device.bus", "usb",
      "device.vendor.id", "0x0d8c", "device.product.id", "0x0102", NULL);
  f.device.iface = SPA_INTERFACE_INIT(SPA_TYPE_INTERFACE_Device,
      SPA_VERSION_DEVICE, &methods, &f);
  spa_hook_list_init(&f.listeners);
  f.loop = pw_main_loop_new(NULL);
  struct pw_context *context = pw_context_new(pw_main_loop_get_loop(f.loop), NULL, 0);
  pw_context_load_module(context, "libpipewire-module-client-device", NULL, NULL);
  f.core = pw_context_connect(context, NULL, 0);
  if (!f.core) return 2;
  f.proxy = pw_core_export(f.core, SPA_TYPE_INTERFACE_Device, &f.props->dict,
      &f.device, 0);
  if (!f.proxy) return 3;
  pw_proxy_add_listener(f.proxy, &f.proxy_listener, &proxy_events, &f);
  pw_loop_add_signal(pw_main_loop_get_loop(f.loop), SIGTERM, quit, f.loop);
  pw_loop_add_signal(pw_main_loop_get_loop(f.loop), SIGINT, quit, f.loop);
  pw_main_loop_run(f.loop);
  pw_proxy_destroy(f.proxy);
  pw_core_disconnect(f.core);
  pw_context_destroy(context);
  pw_main_loop_destroy(f.loop);
  pw_properties_free(f.props);
  return 0;
}
