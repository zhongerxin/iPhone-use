#import <Foundation/Foundation.h>
#import "FBTCPSocket.h"
#import <arpa/inet.h>
#import <sys/socket.h>
#import <unistd.h>
@interface Probe : NSObject <FBTCPSocketDelegate>
@property FBTCPSocket *server;
@end
@implementation Probe
- (void)didClientConnect:(nw_connection_t)client { [self.server writeData:[@"READY\n" dataUsingEncoding:NSUTF8StringEncoding] toClient:client]; }
- (void)client:(nw_connection_t)client didReceiveData:(NSData *)data {}
- (void)didClientDisconnect:(nw_connection_t)client {}
@end
int main(void) { @autoreleasepool {
  Probe *probe = [Probe new]; probe.server = [[FBTCPSocket alloc] initWithPort:0]; probe.server.delegate = probe;
  NSError *error = nil; if (![probe.server startWithError:&error]) { NSLog(@"%@", error); return 1; }
  int fd = socket(AF_INET, SOCK_STREAM, 0); struct sockaddr_in addr = { .sin_family = AF_INET, .sin_port = htons(probe.server.port) }; inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
  if (connect(fd, (struct sockaddr *)&addr, sizeof(addr))) return 2;
  struct timeval timeout = { .tv_sec = 2 }; setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
  char response[10]; ssize_t size = recv(fd, response, sizeof(response), 0); close(fd); if (size != 6) return 3;
  NSData *json = [NSJSONSerialization dataWithJSONObject:FBTCPSocket.listenerBindings options:0 error:&error]; puts([[NSString alloc] initWithData:json encoding:NSUTF8StringEncoding].UTF8String);
  int v6 = socket(AF_INET6, SOCK_STREAM, 0); struct sockaddr_in6 addr6 = { .sin6_family = AF_INET6, .sin6_port = htons(probe.server.port) }; inet_pton(AF_INET6, "::1", &addr6.sin6_addr); int connected6 = connect(v6, (struct sockaddr *)&addr6, sizeof(addr6)); close(v6); if (connected6 == 0) return 4;
  [probe.server stop]; if (FBTCPSocket.listenerBindings.count) return 5;
  for (NSString *bad in @[@"0.0.0.0", @"::", @"::1", @"192.168.1.2"]) { FBTCPSocket *unsafe = [[FBTCPSocket alloc] initWithPort:0]; unsafe.interface = bad; if ([unsafe startWithError:&error]) return 6; }
  return 0;
} }
